"""Plan, checkpoint, resume, and finalize the M2.5 industry acceptance."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from scripts.market_data.industry_classification import (
    HISTORY_START,
    build_intervals,
    build_manifest,
    canonical_scope,
    enrich_interval_names,
    evaluate_industry,
    read_gzip_rows,
    write_gzip_rows,
)
from scripts.market_data.industry_contracts import (
    IndustryNode,
    IndustryScopeSecurity,
    IndustryVerification,
    SwsAssignmentRecord,
)
from scripts.market_data.manifest import sha256
from scripts.market_data.sources.cninfo_industry_source import CninfoIndustrySource
from scripts.market_data.sources.sws_industry_source import SwsIndustrySource
from scripts.market_data.tidb_industry_store import (
    TiDBConfig,
    completed_symbols,
    connect,
    ensure_industry_schema,
    load_base_scope,
    load_industry_intervals,
    load_industry_verifications,
    publish_industry_run,
    publish_symbol_checkpoint,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_BASE_HISTORY_DATASET_ID = (
    "m2-full-2026-07-24-"
    "993df9aab3cbd021a495535c9326eaa79f26f4bbfbe74b28215256e778e517f7-merged"
)
MODE_COUNTS = {"sample": 20, "full": 1403}
MODE_SHARDS = {"sample": 2, "full": 4}
SYMBOL_DEADLINE_SECONDS = 90


class IndustrySymbolTimeout(BaseException):
    pass


@contextmanager
def symbol_deadline(seconds: int) -> Iterator[None]:
    if sys.platform == "win32" or not hasattr(signal, "SIGALRM"):
        yield
        return
    previous = signal.getsignal(signal.SIGALRM)

    def _raise(_signum: int, _frame: Any) -> None:
        raise IndustrySymbolTimeout(f"industry symbol request exceeded {seconds} seconds")

    signal.signal(signal.SIGALRM, _raise)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _progress(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def _scope_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[IndustryScopeSecurity]:
    return [IndustryScopeSecurity.build(row["symbol"], row["ipo_date"], row.get("out_date")) for row in rows]


def _source_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[SwsAssignmentRecord]:
    return [SwsAssignmentRecord.build(
        symbol=row["symbol"],
        source_effective_from=row["source_effective_from"],
        industry_code=row["industry_code"],
        source_updated_at=row.get("source_updated_at"),
    ) for row in rows]


def _nodes_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[IndustryNode]:
    return [IndustryNode(
        node_code=str(row["node_code"]), node_name=str(row["node_name"]),
        parent_code=None if row.get("parent_code") is None else str(row["parent_code"]),
        level=int(row["level"]), standard_name=str(row["standard_name"]),
        standard_code=str(row["standard_code"]),
        termination_date=None if row.get("termination_date") is None else date.fromisoformat(str(row["termination_date"])),
        source=str(row.get("source") or "cninfo_catalog"),
    ) for row in rows]


def _dataset_id(seed: Mapping[str, Any]) -> str:
    return f"m2-industry-{seed['observed_on']}-{sha256(seed)}"


def _plan_seed(
    *,
    base_history_dataset_id: str,
    mode: str,
    observed_on: date,
    scope_sha256: str,
    sws_raw_sha256: str,
    source_assignments_sha256: str,
    nodes_sha256: str,
) -> dict[str, Any]:
    return {
        "base_history_dataset_id": base_history_dataset_id,
        "mode": mode,
        "observed_on": observed_on.isoformat(),
        "scope_sha256": scope_sha256,
        "sws_raw_sha256": sws_raw_sha256,
        "source_assignments_sha256": source_assignments_sha256,
        "nodes_sha256": nodes_sha256,
    }


def build_plan(
    *,
    mode: str,
    observed_on: date,
    base_history_dataset_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    if mode not in MODE_COUNTS:
        raise ValueError(f"unsupported industry acceptance mode: {mode}")
    config = TiDBConfig.from_env()
    connection = connect(config)
    try:
        full_scope = load_base_scope(connection, base_history_dataset_id)
    finally:
        connection.close()
    if len(full_scope) != MODE_COUNTS["full"]:
        raise RuntimeError(f"accepted M2.3 base scope changed: expected 1403, got {len(full_scope)}")
    scope = full_scope if mode == "full" else full_scope[:MODE_COUNTS[mode]]
    scope_symbols = [item.symbol for item in scope]
    sws = SwsIndustrySource().fetch(scope_symbols)
    source_rows = [row for row in sws.rows if row.source_effective_from <= observed_on]
    nodes = list(CninfoIndustrySource().fetch_catalog())
    scope_hash = sha256(canonical_scope(scope))
    source_hash = sha256([row.canonical() for row in source_rows])
    nodes_hash = sha256([row.canonical() for row in nodes])
    seed = _plan_seed(
        base_history_dataset_id=base_history_dataset_id,
        mode=mode,
        observed_on=observed_on,
        scope_sha256=scope_hash,
        sws_raw_sha256=sws.raw_sha256,
        source_assignments_sha256=source_hash,
        nodes_sha256=nodes_hash,
    )
    dataset_id = _dataset_id(seed)
    shard_count = MODE_SHARDS[mode]
    plan = {
        "plan_version": "m2-industry-plan-v1",
        "dataset_id": dataset_id,
        "base_history_dataset_id": base_history_dataset_id,
        "mode": mode,
        "observed_on": observed_on.isoformat(),
        "as_of_date": observed_on.isoformat(),
        "history_start": HISTORY_START.isoformat(),
        "expected_scope_count": MODE_COUNTS[mode],
        "scope_sha256": scope_hash,
        "source_assignments_sha256": source_hash,
        "nodes_sha256": nodes_hash,
        "source_metadata": {
            "sws_assignment_url": sws.source_url,
            "sws_assignment_raw_sha256": sws.raw_sha256,
            "sws_assignment_raw_bytes": sws.raw_bytes,
            "cninfo_catalog_source": "stock_industry_category_cninfo:申银万国行业分类标准",
            "cninfo_change_source": "stock_industry_change_cninfo",
        },
        "shard_count": shard_count,
        "matrix": {"include": [{"shard_index": index} for index in range(shard_count)]},
        "seed": seed,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    write_gzip_rows(output_dir / "scope.json.gz", canonical_scope(scope))
    write_gzip_rows(output_dir / "sws-assignments.json.gz", [row.canonical() for row in source_rows])
    write_gzip_rows(output_dir / "cninfo-nodes.json.gz", [row.canonical() for row in nodes])
    _progress(
        "industry_plan_ready", dataset_id=dataset_id, mode=mode, scope_count=len(scope),
        source_assignment_count=len(source_rows), node_count=len(nodes), shard_count=shard_count,
    )
    return plan


def load_plan(input_dir: Path) -> tuple[dict[str, Any], list[IndustryScopeSecurity], list[SwsAssignmentRecord], list[IndustryNode]]:
    plan = json.loads((input_dir / "plan.json").read_text(encoding="utf-8"))
    scope = _scope_from_rows(read_gzip_rows(input_dir / "scope.json.gz"))
    source_rows = _source_from_rows(read_gzip_rows(input_dir / "sws-assignments.json.gz"))
    nodes = _nodes_from_rows(read_gzip_rows(input_dir / "cninfo-nodes.json.gz"))
    actual = {
        "scope_sha256": sha256(canonical_scope(scope)),
        "source_assignments_sha256": sha256([row.canonical() for row in source_rows]),
        "nodes_sha256": sha256([row.canonical() for row in nodes]),
    }
    mismatches = {key: {"plan": plan.get(key), "artifact": value} for key, value in actual.items() if plan.get(key) != value}
    if mismatches:
        raise RuntimeError(f"industry frozen-plan hash mismatch: {mismatches}")
    if _dataset_id(plan["seed"]) != plan["dataset_id"]:
        raise RuntimeError("industry plan dataset id does not match its deterministic seed")
    if len(scope) != int(plan["expected_scope_count"]):
        raise RuntimeError("industry plan scope count does not reconcile")
    return plan, scope, source_rows, nodes


def _shard_scope(scope: Sequence[IndustryScopeSecurity], shard_index: int, shard_count: int) -> list[IndustryScopeSecurity]:
    if not 0 <= shard_index < shard_count:
        raise ValueError("industry shard index is outside the plan")
    return [item for position, item in enumerate(scope) if position % shard_count == shard_index]


def run_shard(*, input_dir: Path, shard_index: int, attempts: int = 3) -> dict[str, Any]:
    if attempts < 1 or attempts > 3:
        raise ValueError("industry symbol attempts must be between 1 and 3")
    plan, scope, source_rows, nodes = load_plan(input_dir)
    shard_count = int(plan["shard_count"])
    shard_scope = _shard_scope(scope, shard_index, shard_count)
    dataset_id = str(plan["dataset_id"])
    config = TiDBConfig.from_env()
    connection = connect(config)
    try:
        ensure_industry_schema(connection)
        done = completed_symbols(connection, dataset_id)
    finally:
        connection.close()
    pending = [item for item in shard_scope if item.symbol not in done]
    _progress(
        "industry_shard_started", dataset_id=dataset_id, shard_index=shard_index,
        shard_count=shard_count, shard_symbols=len(shard_scope), resumed_symbols=len(shard_scope) - len(pending),
        pending_symbols=len(pending),
    )
    source = CninfoIndustrySource()
    failed = 0
    as_of = date.fromisoformat(plan["as_of_date"])
    observed_on = date.fromisoformat(plan["observed_on"])
    source_by_symbol: dict[str, list[SwsAssignmentRecord]] = {}
    for security in shard_scope:
        source_by_symbol[security.symbol] = [row for row in source_rows if row.symbol == security.symbol]
    for position, security in enumerate(pending, start=1):
        final_error: Exception | str | None = None
        verification_rows: Sequence[IndustryVerification] = ()
        intervals = build_intervals(
            [security], source_by_symbol.get(security.symbol, []), observed_on=observed_on,
            as_of_date=as_of, history_start=date.fromisoformat(plan["history_start"]),
        )
        for attempt in range(1, attempts + 1):
            try:
                with symbol_deadline(SYMBOL_DEADLINE_SECONDS):
                    verification_rows = source.fetch_changes(security.symbol, date(1990, 1, 1), as_of)
                enriched = enrich_interval_names(intervals, verification_rows, nodes)
                connection = connect(config)
                try:
                    publish_symbol_checkpoint(
                        connection, dataset_id=dataset_id, symbol=security.symbol,
                        shard_index=shard_index, intervals=enriched,
                        verifications=verification_rows, status="succeeded",
                    )
                finally:
                    connection.close()
                break
            except IndustrySymbolTimeout as error:
                final_error = RuntimeError(str(error))
            except Exception as error:
                final_error = error
            if attempt < attempts:
                _progress(
                    "industry_symbol_retry", symbol=security.symbol, shard_index=shard_index,
                    attempt=attempt, error=f"{type(final_error).__name__}: {final_error}",
                )
                time.sleep(min(2 ** (attempt - 1), 2))
        else:
            failed += 1
            connection = connect(config)
            try:
                publish_symbol_checkpoint(
                    connection, dataset_id=dataset_id, symbol=security.symbol,
                    shard_index=shard_index, intervals=[], verifications=[], status="failed",
                    error=final_error or "unknown CNINFO verification failure",
                )
            finally:
                connection.close()
        _progress(
            "industry_symbol_completed", symbol=security.symbol, shard_index=shard_index,
            completed=position, total=len(pending), failed=failed,
        )
    result = {
        "dataset_id": dataset_id, "shard_index": shard_index, "shard_count": shard_count,
        "pending_symbols": len(pending), "failed_symbols": failed,
        "succeeded_or_resumed_symbols": len(shard_scope) - failed,
    }
    _progress("industry_shard_completed", **result)
    return result


def finalize(*, input_dir: Path, output_dir: Path) -> dict[str, Any]:
    plan, scope, source_rows, nodes = load_plan(input_dir)
    config = TiDBConfig.from_env()
    connection = connect(config)
    try:
        intervals = load_industry_intervals(connection, plan["dataset_id"])
        verifications = load_industry_verifications(connection, plan["dataset_id"])
    finally:
        connection.close()
    gates = evaluate_industry(
        scope=scope, source_rows=source_rows, intervals=intervals,
        verifications=verifications, nodes=nodes,
        history_start=date.fromisoformat(plan["history_start"]),
        as_of_date=date.fromisoformat(plan["as_of_date"]),
        expected_scope_count=int(plan["expected_scope_count"]),
    )
    manifest = build_manifest(
        dataset_id=plan["dataset_id"], base_history_dataset_id=plan["base_history_dataset_id"],
        mode=plan["mode"], observed_on=date.fromisoformat(plan["observed_on"]),
        as_of_date=date.fromisoformat(plan["as_of_date"]),
        history_start=date.fromisoformat(plan["history_start"]), scope=scope,
        source_rows=source_rows, intervals=intervals, verifications=verifications,
        nodes=nodes, gates=gates, source_metadata=plan["source_metadata"],
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    if not manifest["accepted"]:
        failed = [gate["name"] for gate in manifest["gates"] if gate["critical"] and not gate["passed"]]
        _progress("industry_rejected", dataset_id=plan["dataset_id"], failed_critical_gates=failed)
        return {"dataset_id": plan["dataset_id"], "accepted": False, "failed_critical_gates": failed}
    connection = connect(config)
    try:
        result = publish_industry_run(
            connection, manifest, scope=scope, source_rows=source_rows, intervals=intervals,
            verifications=verifications, nodes=nodes,
        )
    finally:
        connection.close()
    _progress("industry_accepted", **result)
    return {**result, "manifest": manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description="M2.5 point-in-time industry acceptance")
    parser.add_argument("--operation", required=True, choices=("plan", "shard", "finalize"))
    parser.add_argument("--mode", choices=tuple(MODE_COUNTS), default="sample")
    parser.add_argument("--observed-on", type=date.fromisoformat)
    parser.add_argument("--base-history-dataset-id", default=DEFAULT_BASE_HISTORY_DATASET_ID)
    parser.add_argument("--input-dir", type=Path, default=Path("industry-plan"))
    parser.add_argument("--output-dir", type=Path, default=Path("industry-acceptance"))
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    if args.operation == "plan":
        plan = build_plan(
            mode=args.mode, observed_on=args.observed_on or datetime.now(SHANGHAI).date(),
            base_history_dataset_id=args.base_history_dataset_id, output_dir=args.output_dir,
        )
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.operation == "shard":
        if args.shard_index is None:
            parser.error("--shard-index is required for shard operation")
        result = run_shard(input_dir=args.input_dir, shard_index=args.shard_index, attempts=args.attempts)
        return 0 if result["failed_symbols"] == 0 else 2
    result = finalize(input_dir=args.input_dir, output_dir=args.output_dir)
    return 0 if result.get("accepted") else 2


if __name__ == "__main__":
    raise SystemExit(main())

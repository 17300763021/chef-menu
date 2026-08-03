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
    EXCLUDED_DELISTED_NO_HISTORY,
    INDUSTRY_SCHEMA_VERSION,
    IndustryDelistingEvidence,
    IndustryExclusion,
    IndustryNode,
    IndustryScopeSecurity,
    IndustryVerification,
    SwsAssignmentRecord,
)
from scripts.market_data.manifest import sha256
from scripts.market_data.sample_capture import SAMPLE_SYMBOLS
from scripts.market_data.sources.cninfo_industry_source import (
    CninfoIndustrySource,
    assignments_from_cninfo_changes,
)
from scripts.market_data.sources.exchange_delisting_source import ExchangeDelistingSource
from scripts.market_data.tidb_industry_store import (
    TiDBConfig,
    completed_symbols,
    connect,
    ensure_industry_schema,
    load_accepted_industry_nodes,
    load_base_scope,
    load_industry_exclusions,
    load_industry_intervals,
    load_industry_source_assignments,
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
PLAN_VERSION = "m2-industry-plan-v3"


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


def _nodes_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[IndustryNode]:
    return [IndustryNode(
        node_code=str(row["node_code"]), node_name=str(row["node_name"]),
        parent_code=None if row.get("parent_code") is None else str(row["parent_code"]),
        level=int(row["level"]), standard_name=str(row["standard_name"]),
        standard_code=str(row["standard_code"]),
        termination_date=None if row.get("termination_date") is None else date.fromisoformat(str(row["termination_date"])),
        source=str(row.get("source") or "cninfo_catalog"),
    ) for row in rows]


def _delistings_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[IndustryDelistingEvidence]:
    return [IndustryDelistingEvidence.build(**row) for row in rows]


def _dataset_id(seed: Mapping[str, Any]) -> str:
    return f"m2-industry-{seed['observed_on']}-{sha256(seed)}"


def _plan_seed(
    *,
    base_history_dataset_id: str,
    mode: str,
    observed_on: date,
    scope_sha256: str,
    nodes_sha256: str,
    delisting_inventory_sha256: str,
    catalog_evidence_dataset_id: str | None,
) -> dict[str, Any]:
    return {
        "plan_version": PLAN_VERSION,
        "schema_version": INDUSTRY_SCHEMA_VERSION,
        "base_history_dataset_id": base_history_dataset_id,
        "mode": mode,
        "observed_on": observed_on.isoformat(),
        "scope_sha256": scope_sha256,
        "nodes_sha256": nodes_sha256,
        "delisting_inventory_sha256": delisting_inventory_sha256,
        "catalog_evidence_dataset_id": catalog_evidence_dataset_id,
        "primary_source": "cninfo_official_api",
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
        accepted_catalog = load_accepted_industry_nodes(connection, observed_on)
    finally:
        connection.close()
    if len(full_scope) != MODE_COUNTS["full"]:
        raise RuntimeError(f"accepted M2.3 base scope changed: expected 1403, got {len(full_scope)}")
    if mode == "full":
        scope = full_scope
    else:
        full_by_symbol = {item.symbol: item for item in full_scope}
        missing_sample = [symbol for symbol in SAMPLE_SYMBOLS if symbol not in full_by_symbol]
        if missing_sample:
            raise RuntimeError(f"accepted M2.3 base scope is missing fixed sample symbols: {missing_sample}")
        scope = [full_by_symbol[symbol] for symbol in SAMPLE_SYMBOLS]
    if accepted_catalog is None:
        nodes = list(CninfoIndustrySource(attempts=3).fetch_catalog())
        catalog_evidence_dataset_id = None
        catalog_capture_mode = "live_cninfo"
    else:
        nodes, catalog_evidence_dataset_id = accepted_catalog
        catalog_capture_mode = "accepted_same_date_tidb_reuse"
    nodes = sorted(nodes, key=lambda row: (row.level, row.node_code))
    scope_symbols = {item.symbol for item in scope}
    delistings = [
        row for row in ExchangeDelistingSource(attempts=3).fetch()
        if row.symbol in scope_symbols
    ]
    scope_hash = sha256(canonical_scope(scope))
    nodes_hash = sha256([row.canonical() for row in nodes])
    delistings_hash = sha256([row.canonical() for row in delistings])
    seed = _plan_seed(
        base_history_dataset_id=base_history_dataset_id,
        mode=mode,
        observed_on=observed_on,
        scope_sha256=scope_hash,
        nodes_sha256=nodes_hash,
        delisting_inventory_sha256=delistings_hash,
        catalog_evidence_dataset_id=catalog_evidence_dataset_id,
    )
    dataset_id = _dataset_id(seed)
    shard_count = MODE_SHARDS[mode]
    plan = {
        "plan_version": PLAN_VERSION,
        "dataset_id": dataset_id,
        "base_history_dataset_id": base_history_dataset_id,
        "mode": mode,
        "observed_on": observed_on.isoformat(),
        "as_of_date": observed_on.isoformat(),
        "history_start": HISTORY_START.isoformat(),
        "expected_scope_count": MODE_COUNTS[mode],
        "scope_sha256": scope_hash,
        "nodes_sha256": nodes_hash,
        "delisting_inventory_count": len(delistings),
        "delisting_inventory_sha256": delistings_hash,
        "catalog_evidence_dataset_id": catalog_evidence_dataset_id,
        "source_metadata": {
            "primary_assignment_source": "CNINFO p_stock2110 via stock_industry_change_cninfo",
            "classification_catalog_source": "CNINFO p_public0002 via stock_industry_category_cninfo",
            "primary_transport": "verified HTTPS",
            "sws_workbook_role": "optional independent diagnostic; unavailable with verified TLS",
            "delisting_sources": "SZSE and SSE official terminated-listing inventories via AKShare",
            "delisting_inventory_count": len(delistings),
            "delisting_inventory_sha256": delistings_hash,
            "catalog_capture_mode": catalog_capture_mode,
            "catalog_evidence_dataset_id": catalog_evidence_dataset_id,
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
    write_gzip_rows(output_dir / "cninfo-nodes.json.gz", [row.canonical() for row in nodes])
    write_gzip_rows(output_dir / "exchange-delistings.json.gz", [row.canonical() for row in delistings])
    _progress(
        "industry_plan_ready", dataset_id=dataset_id, mode=mode, scope_count=len(scope),
        node_count=len(nodes), delisting_count=len(delistings), shard_count=shard_count,
    )
    return plan


def load_plan(
    input_dir: Path,
) -> tuple[
    dict[str, Any],
    list[IndustryScopeSecurity],
    list[IndustryNode],
    list[IndustryDelistingEvidence],
]:
    plan = json.loads((input_dir / "plan.json").read_text(encoding="utf-8"))
    if plan.get("plan_version") != PLAN_VERSION:
        raise RuntimeError(f"unsupported industry plan version: {plan.get('plan_version')!r}")
    scope = _scope_from_rows(read_gzip_rows(input_dir / "scope.json.gz"))
    nodes = _nodes_from_rows(read_gzip_rows(input_dir / "cninfo-nodes.json.gz"))
    delistings = _delistings_from_rows(read_gzip_rows(input_dir / "exchange-delistings.json.gz"))
    actual = {
        "scope_sha256": sha256(canonical_scope(scope)),
        "nodes_sha256": sha256([row.canonical() for row in nodes]),
        "delisting_inventory_sha256": sha256([row.canonical() for row in delistings]),
    }
    mismatches = {key: {"plan": plan.get(key), "artifact": value} for key, value in actual.items() if plan.get(key) != value}
    if mismatches:
        raise RuntimeError(f"industry frozen-plan hash mismatch: {mismatches}")
    if _dataset_id(plan["seed"]) != plan["dataset_id"]:
        raise RuntimeError("industry plan dataset id does not match its deterministic seed")
    if len(scope) != int(plan["expected_scope_count"]):
        raise RuntimeError("industry plan scope count does not reconcile")
    if len(delistings) != int(plan["delisting_inventory_count"]):
        raise RuntimeError("industry delisting inventory count does not reconcile")
    if any(row.symbol not in {item.symbol for item in scope} for row in delistings):
        raise RuntimeError("industry delisting inventory contains symbols outside the frozen scope")
    return plan, scope, nodes, delistings


def _shard_scope(scope: Sequence[IndustryScopeSecurity], shard_index: int, shard_count: int) -> list[IndustryScopeSecurity]:
    if not 0 <= shard_index < shard_count:
        raise ValueError("industry shard index is outside the plan")
    return [item for position, item in enumerate(scope) if position % shard_count == shard_index]


def _delisted_no_history_exclusion(
    security: IndustryScopeSecurity,
    *,
    as_of_date: date,
    confirmed_empty_responses: int = 2,
    delisting_evidence: IndustryDelistingEvidence | None = None,
) -> IndustryExclusion | None:
    if confirmed_empty_responses < 2:
        return None
    if security.out_date is not None and security.out_date <= as_of_date:
        out_date = security.out_date
        delisting_source = "m2_history_security_reference"
    elif delisting_evidence is not None and delisting_evidence.delisted_on <= as_of_date:
        if delisting_evidence.symbol != security.symbol:
            raise ValueError("official delisting evidence does not match the security")
        out_date = delisting_evidence.delisted_on
        delisting_source = delisting_evidence.source
    else:
        return None
    return IndustryExclusion.build(
        symbol=security.symbol,
        out_date=out_date,
        confirmed_empty_responses=confirmed_empty_responses,
        delisting_source=delisting_source,
    )


def run_shard(*, input_dir: Path, shard_index: int, attempts: int = 3) -> dict[str, Any]:
    if attempts < 1 or attempts > 3:
        raise ValueError("industry symbol attempts must be between 1 and 3")
    plan, scope, nodes, delistings = load_plan(input_dir)
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
    excluded = 0
    as_of = date.fromisoformat(plan["as_of_date"])
    captured_on = datetime.now(SHANGHAI).date()
    delisting_by_symbol = {row.symbol: row for row in delistings}
    for position, security in enumerate(pending, start=1):
        final_error: Exception | str | None = None
        verification_rows: Sequence[IndustryVerification] = ()
        symbol_source_rows: Sequence[SwsAssignmentRecord] = ()
        intervals = []
        confirmed_empty_responses = 0
        for attempt in range(1, attempts + 1):
            try:
                with symbol_deadline(SYMBOL_DEADLINE_SECONDS):
                    verification_rows = source.fetch_changes(security.symbol, date(1990, 1, 1), as_of)
                if not verification_rows:
                    confirmed_empty_responses += 1
                    exclusion = _delisted_no_history_exclusion(
                        security,
                        as_of_date=as_of,
                        confirmed_empty_responses=confirmed_empty_responses,
                        delisting_evidence=delisting_by_symbol.get(security.symbol),
                    )
                    if exclusion is not None:
                        connection = connect(config)
                        try:
                            publish_symbol_checkpoint(
                                connection, dataset_id=dataset_id, symbol=security.symbol,
                                shard_index=shard_index, source_rows=[], intervals=[], verifications=[],
                                status=EXCLUDED_DELISTED_NO_HISTORY, exclusion=exclusion,
                            )
                        finally:
                            connection.close()
                        excluded += 1
                        _progress(
                            "industry_symbol_excluded", symbol=security.symbol,
                            shard_index=shard_index, out_date=exclusion.out_date.isoformat(),
                            reason=exclusion.reason,
                        )
                        break
                    if (
                        (security.out_date is not None and security.out_date <= as_of)
                        or security.symbol in delisting_by_symbol
                    ):
                        raise RuntimeError(
                            f"CNINFO returned empty Shenwan industry history for delisted {security.symbol}; "
                            "a second empty response is required before exclusion"
                        )
                    raise RuntimeError(f"CNINFO returned no Shenwan industry history for {security.symbol}")
                symbol_source_rows = assignments_from_cninfo_changes(list(verification_rows))
                intervals = build_intervals(
                    [security], symbol_source_rows, observed_on=captured_on,
                    as_of_date=as_of, history_start=date.fromisoformat(plan["history_start"]),
                )
                if not intervals:
                    raise RuntimeError(f"CNINFO industry history produced no valid interval for {security.symbol}")
                enriched = enrich_interval_names(intervals, verification_rows, nodes)
                connection = connect(config)
                try:
                    publish_symbol_checkpoint(
                        connection, dataset_id=dataset_id, symbol=security.symbol,
                        shard_index=shard_index, source_rows=symbol_source_rows, intervals=enriched,
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
                    shard_index=shard_index, source_rows=[], intervals=[], verifications=[], status="failed",
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
        "excluded_symbols": excluded,
        "succeeded_or_resumed_symbols": len(shard_scope) - failed,
    }
    _progress("industry_shard_completed", **result)
    return result


def finalize(*, input_dir: Path, output_dir: Path) -> dict[str, Any]:
    plan, scope, nodes, delistings = load_plan(input_dir)
    config = TiDBConfig.from_env()
    connection = connect(config)
    try:
        source_rows = load_industry_source_assignments(connection, plan["dataset_id"])
        intervals = load_industry_intervals(connection, plan["dataset_id"])
        verifications = load_industry_verifications(connection, plan["dataset_id"])
        exclusions = load_industry_exclusions(connection, plan["dataset_id"])
    finally:
        connection.close()
    gates = evaluate_industry(
        scope=scope, source_rows=source_rows, intervals=intervals,
        verifications=verifications, nodes=nodes,
        history_start=date.fromisoformat(plan["history_start"]),
        as_of_date=date.fromisoformat(plan["as_of_date"]),
        expected_scope_count=int(plan["expected_scope_count"]),
        exclusions=exclusions,
        delisting_evidence=delistings,
    )
    knowledge_observed_on = max(
        (row.known_from for row in intervals),
        default=date.fromisoformat(plan["observed_on"]),
    )
    manifest = build_manifest(
        dataset_id=plan["dataset_id"], base_history_dataset_id=plan["base_history_dataset_id"],
        mode=plan["mode"], observed_on=knowledge_observed_on,
        as_of_date=date.fromisoformat(plan["as_of_date"]),
        history_start=date.fromisoformat(plan["history_start"]), scope=scope,
        source_rows=source_rows, intervals=intervals, verifications=verifications,
        nodes=nodes, gates=gates, source_metadata=plan["source_metadata"], exclusions=exclusions,
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
            verifications=verifications, nodes=nodes, exclusions=exclusions,
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

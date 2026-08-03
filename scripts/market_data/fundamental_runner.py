"""Checkpointed M2 fundamental capture and deterministic finalization."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from scripts.market_data.fundamental_contracts import (
    FUNDAMENTAL_SCHEMA_VERSION,
    FundamentalFact,
    FundamentalReport,
)
from scripts.market_data.fundamental_quality_gates import evaluate_fundamentals
from scripts.market_data.manifest import sha256
from scripts.market_data.quality_gates import accepted
from scripts.market_data.sample_capture import SAMPLE_SYMBOLS
from scripts.market_data.sources.cninfo_announcement_source import CninfoAnnouncementSource
from scripts.market_data.sources.eastmoney_fundamental_source import EastmoneyFundamentalSource
from scripts.market_data.tidb_fundamental_store import (
    TiDBConfig,
    connect,
    ensure_fundamental_schema,
    load_dataset,
    publish_run,
    publish_symbol_checkpoint,
)
from scripts.market_data.tidb_industry_store import load_base_scope


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_BASE_HISTORY_DATASET_ID = (
    "m2-full-2026-07-24-"
    "993df9aab3cbd021a495535c9326eaa79f26f4bbfbe74b28215256e778e517f7-merged"
)
HISTORY_START = date(2017, 1, 1)
MODE_COUNTS = {"sample": 20, "full": 1403}


def _progress(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def _scope(connection: Any, base_id: str, mode: str):
    full = load_base_scope(connection, base_id)
    if len(full) != MODE_COUNTS["full"]:
        raise RuntimeError(f"accepted M2.3 scope changed: expected 1403, got {len(full)}")
    if mode == "full":
        return full
    by_symbol = {row.symbol: row for row in full}
    missing = [symbol for symbol in SAMPLE_SYMBOLS if symbol not in by_symbol]
    if missing:
        raise RuntimeError(f"accepted history scope misses sample symbols: {missing}")
    return [by_symbol[symbol] for symbol in SAMPLE_SYMBOLS]


def dataset_id(*, base_id: str, mode: str, as_of_date: date, symbols: list[str]) -> str:
    seed = {
        "schema_version": FUNDAMENTAL_SCHEMA_VERSION,
        "base_history_dataset_id": base_id,
        "mode": mode,
        "as_of_date": as_of_date.isoformat(),
        "history_start": HISTORY_START.isoformat(),
        "symbols": sorted(symbols),
    }
    return f"m2-fundamental-{as_of_date.isoformat()}-{sha256(seed)}"


def capture(
    *,
    mode: str,
    as_of_date: date,
    base_id: str,
    shard_index: int,
    shard_count: int,
    attempts: int,
) -> dict[str, Any]:
    config = TiDBConfig.from_env()
    connection = connect(config)
    try:
        ensure_fundamental_schema(connection)
        scope = _scope(connection, base_id, mode)
        symbols = [row.symbol for row in scope]
        run_id = dataset_id(base_id=base_id, mode=mode, as_of_date=as_of_date, symbols=symbols)
        with connection.cursor() as cursor:
            cursor.execute("SELECT symbol,status FROM m2_fundamental_symbol_checkpoints WHERE dataset_id=%s", (run_id,))
            completed = {str(symbol) for symbol, status in cursor.fetchall() if str(status) in {"succeeded", "excluded"}}
    finally:
        connection.close()
    selected = [row for position, row in enumerate(scope) if position % shard_count == shard_index]
    primary = EastmoneyFundamentalSource(attempts=attempts)
    verifier = CninfoAnnouncementSource(attempts=1)
    succeeded = failed = excluded = 0
    for security in selected:
        if security.symbol in completed:
            _progress("fundamental_symbol_reused", symbol=security.symbol)
            succeeded += 1
            continue
        try:
            reports, facts = primary.fetch(
                security.symbol,
                history_start=HISTORY_START,
                as_of_date=as_of_date,
                delisted=security.out_date is not None and security.out_date <= as_of_date,
            )
            if not reports or not facts:
                if security.out_date is not None and security.out_date <= as_of_date:
                    checkpoint_connection = connect(config)
                    try:
                        publish_symbol_checkpoint(checkpoint_connection, dataset_id=run_id, symbol=security.symbol, status="excluded")
                    finally:
                        checkpoint_connection.close()
                    excluded += 1
                    _progress("fundamental_symbol_excluded", symbol=security.symbol, reason="delisted_source_empty")
                    continue
                raise RuntimeError("no eligible financial reports or facts")
            latest_notice = max(row.notice_date for row in reports)
            try:
                verifications = verifier.fetch_near(security.symbol, latest_notice)
            except Exception as verification_error:  # diagnostic only; primary dates remain explicit.
                verifications = ()
                _progress("fundamental_verification_unavailable", symbol=security.symbol, error=str(verification_error))
            checkpoint_connection = connect(config)
            try:
                publish_symbol_checkpoint(
                    checkpoint_connection,
                    dataset_id=run_id,
                    symbol=security.symbol,
                    status="succeeded",
                    reports=reports,
                    facts=facts,
                    verifications=verifications,
                )
            finally:
                checkpoint_connection.close()
            succeeded += 1
            _progress("fundamental_symbol_completed", symbol=security.symbol, reports=len(reports), facts=len(facts))
        except Exception as error:  # noqa: BLE001 - persisted for resumable cloud repair.
            failed_connection = connect(config)
            try:
                publish_symbol_checkpoint(
                    failed_connection, dataset_id=run_id, symbol=security.symbol, status="failed", error=error,
                )
            finally:
                failed_connection.close()
            failed += 1
            _progress("fundamental_symbol_failed", symbol=security.symbol, error=f"{type(error).__name__}: {error}")
    return {"dataset_id": run_id, "selected": len(selected), "succeeded": succeeded, "excluded": excluded, "failed": failed}


def _report_from_db(row: Mapping[str, Any]) -> FundamentalReport:
    return FundamentalReport(
        symbol=str(row["symbol"]), statement_type=str(row["statement_type"]),
        report_date=date.fromisoformat(str(row["report_date"])),
        notice_date=date.fromisoformat(str(row["notice_date"])),
        update_date=date.fromisoformat(str(row["update_date"])),
        effective_on=date.fromisoformat(str(row["effective_on"])),
        report_type=str(row["report_type"]), currency=str(row["currency"]),
        organization_type=str(row["organization_type"]), source=str(row["source"]),
        source_row_sha256=str(row["source_row_sha256"]), schema_version=str(row["schema_version"]),
    )


def _fact_from_db(row: Mapping[str, Any]) -> FundamentalFact:
    return FundamentalFact(
        report_version_id=str(row["report_version_id"]), symbol=str(row["symbol"]),
        statement_type=str(row["statement_type"]), report_date=date.fromisoformat(str(row["report_date"])),
        effective_on=date.fromisoformat(str(row["effective_on"])), metric_code=str(row["metric_code"]),
        value=Decimal(str(row["metric_value"])), unit=str(row["unit"]),
    )


def finalize(*, mode: str, as_of_date: date, base_id: str, output_dir: Path) -> dict[str, Any]:
    connection = connect(TiDBConfig.from_env())
    try:
        ensure_fundamental_schema(connection)
        scope = _scope(connection, base_id, mode)
        symbols = [row.symbol for row in scope]
        run_id = dataset_id(base_id=base_id, mode=mode, as_of_date=as_of_date, symbols=symbols)
        report_dicts, fact_dicts, checkpoints = load_dataset(connection, run_id)
        status = {str(row["symbol"]): str(row["status"]) for row in checkpoints}
        missing = sorted(set(symbols) - set(status))
        failed = sorted(symbol for symbol, value in status.items() if value == "failed")
        if missing:
            raise RuntimeError(f"fundamental capture inventory incomplete; missing={missing[:20]}")
        reports = [_report_from_db(row) for row in report_dicts]
        facts = [_fact_from_db(row) for row in fact_dicts]
        succeeded = {symbol for symbol, value in status.items() if value == "succeeded"}
        excluded = {symbol for symbol, value in status.items() if value == "excluded"}
        gates = evaluate_fundamentals(
            expected_symbols=symbols, reports=reports, facts=facts,
            successful_symbols=succeeded, excluded_symbols=excluded,
        )
        report_rows = [row.canonical() for row in sorted(reports, key=lambda value: value.key)]
        fact_rows = [row.canonical() for row in sorted(facts, key=lambda value: value.key)]
        quality_rows = [row.canonical() for row in gates]
        verification_count = sum(int(row["verification_count"]) for row in checkpoints)
        manifest = {
            "manifest_version": "m2-fundamental-manifest-v1",
            "schema_version": FUNDAMENTAL_SCHEMA_VERSION,
            "dataset_id": run_id,
            "base_history_dataset_id": base_id,
            "mode": mode,
            "as_of_date": as_of_date.isoformat(),
            "history_start": HISTORY_START.isoformat(),
            "authoritative": False,
            "simulation_orders_allowed": False,
            "knowledge_boundary": "facts are usable no earlier than max(NOTICE_DATE, UPDATE_DATE); initial capture cannot reconstruct superseded pre-revision values",
            "expected_symbol_count": len(symbols),
            "successful_symbol_count": len(succeeded),
            "excluded_symbol_count": len(excluded),
            "failed_symbol_count": len(failed),
            "failed_symbols": failed,
            "report_count": len(reports),
            "fact_count": len(facts),
            "verification_count": verification_count,
            "reports_sha256": sha256(report_rows),
            "facts_sha256": sha256(fact_rows),
            "quality_sha256": sha256(quality_rows),
            "accepted": accepted(gates),
            "gates": quality_rows,
        }
        if not manifest["accepted"]:
            raise RuntimeError(f"fundamental critical quality gate failed: {[g.name for g in gates if g.critical and not g.passed]}")
        result = publish_run(connection, manifest)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        _progress("fundamental_finalized", **result, reports=len(reports), facts=len(facts))
        return manifest
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", choices=("capture", "finalize"), required=True)
    parser.add_argument("--mode", choices=tuple(MODE_COUNTS), default="sample")
    parser.add_argument("--as-of-date", default="")
    parser.add_argument("--base-history-dataset-id", default=DEFAULT_BASE_HISTORY_DATASET_ID)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, default=Path("fundamental-acceptance"))
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of_date) if args.as_of_date else datetime.now(SHANGHAI).date()
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        raise SystemExit("invalid fundamental shard coordinates")
    if args.operation == "capture":
        result = capture(mode=args.mode, as_of_date=as_of, base_id=args.base_history_dataset_id,
                         shard_index=args.shard_index, shard_count=args.shard_count, attempts=args.attempts)
        _progress("fundamental_capture_completed", **result)
        return 0
    finalize(mode=args.mode, as_of_date=as_of, base_id=args.base_history_dataset_id, output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded flow source admission that publishes missingness instead of fabricated numbers."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from scripts.market_data.manifest import sha256
from scripts.market_data.sample_capture import SAMPLE_SYMBOLS
from scripts.market_data.tidb_flow_store import TiDBConfig, connect, ensure_flow_schema, publish_flow_run
from scripts.market_data.verified_flow import FLOW_SCHEMA_VERSION, ExactDateFlowSource


def run(*, business_date: date, output_dir: Path) -> dict:
    source = ExactDateFlowSource()
    facts = []
    checkpoints = []
    for symbol in SAMPLE_SYMBOLS:
        try:
            fact = source.fetch(symbol, business_date)
            facts.append(fact)
            checkpoints.append({"symbol": symbol, "status": "succeeded"})
        except Exception as error:  # noqa: BLE001 - unavailability is explicit, never converted to zero.
            checkpoints.append({
                "symbol": symbol,
                "status": "unavailable",
                "error_class": type(error).__name__,
                "error_message": str(error)[:2000],
            })
    fact_rows = [row.canonical() for row in sorted(facts, key=lambda row: row.key)]
    coverage_bps = len(facts) * 10000 // len(SAMPLE_SYMBOLS)
    data_available = coverage_bps >= 9800
    seed = {"schema_version": FLOW_SCHEMA_VERSION, "business_date": business_date.isoformat(), "symbols": list(SAMPLE_SYMBOLS)}
    manifest = {
        "manifest_version": "m2-flow-boundary-manifest-v1",
        "schema_version": FLOW_SCHEMA_VERSION,
        "dataset_id": f"m2-flow-{business_date.isoformat()}-{sha256(seed)}",
        "business_date": business_date.isoformat(),
        "expected_symbol_count": len(SAMPLE_SYMBOLS),
        "available_symbol_count": len(facts),
        "coverage_percent": f"{coverage_bps / 100:.2f}",
        "data_available": data_available,
        "boundary_accepted": True,
        "authoritative": False,
        "simulation_orders_allowed": False,
        "missing_value_policy": "missing remains unavailable; no zero, neutral-score, or stale-date substitution",
        "strategy_policy": "flow factor is disabled and remaining verified weights must be renormalized when data_available=false",
        "facts_sha256": sha256(fact_rows),
        "checkpoint_sha256": sha256(checkpoints),
    }
    connection = connect(TiDBConfig.from_env())
    try:
        ensure_flow_schema(connection)
        result = publish_flow_run(connection, manifest=manifest, facts=facts, checkpoints=checkpoints)
    finally:
        connection.close()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps({"event": "flow_boundary_finalized", **result, "data_available": data_available}, ensure_ascii=False), flush=True)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("flow-acceptance"))
    args = parser.parse_args()
    run(business_date=date.fromisoformat(args.business_date), output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

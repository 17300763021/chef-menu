"""Capture, validate, and publish CSI 300/500 benchmark histories."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.market_data.index_bars import INDEX_CODES, INDEX_SCHEMA_VERSION, IndexBarSource
from scripts.market_data.index_quality_gates import evaluate_index_bars
from scripts.market_data.manifest import sha256
from scripts.market_data.quality_gates import accepted
from scripts.market_data.tidb_index_store import TiDBConfig, connect, ensure_index_schema, publish_index_run


SHANGHAI = ZoneInfo("Asia/Shanghai")
HISTORY_START = date(2018, 1, 1)
KNOWN_CSI_NON_SESSION_ROWS = {date(2018, 1, 1)}


def run(*, business_end: date, output_dir: Path) -> dict:
    source = IndexBarSource()
    primary = []
    verification = []
    for code in INDEX_CODES:
        first, second = source.fetch(code, HISTORY_START, business_end)
        primary.extend(first)
        verification.extend(second)
    date_sets = [
        {row.business_date for row in rows if row.index_code == code}
        for rows in (primary, verification) for code in INDEX_CODES
    ]
    expected_sessions = set.intersection(*date_sets)
    disagreements = set.union(*date_sets) - expected_sessions
    if disagreements - KNOWN_CSI_NON_SESSION_ROWS:
        raise RuntimeError(f"index sources disagree on unexpected business dates: {sorted(disagreements)[:20]}")
    primary = [row for row in primary if row.business_date in expected_sessions]
    verification = [row for row in verification if row.business_date in expected_sessions]
    gates = evaluate_index_bars(primary, verification, expected_sessions)
    primary_rows = [row.canonical() for row in sorted(primary, key=lambda row: row.key)]
    verification_rows = [row.canonical() for row in sorted(verification, key=lambda row: row.key)]
    quality_rows = [gate.canonical() for gate in gates]
    seed = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "business_start": HISTORY_START.isoformat(),
        "business_end": business_end.isoformat(),
        "primary_sha256": sha256(primary_rows),
        "verification_sha256": sha256(verification_rows),
    }
    manifest = {
        "manifest_version": "m2-index-manifest-v1",
        "schema_version": INDEX_SCHEMA_VERSION,
        "dataset_id": f"m2-index-{business_end.isoformat()}-{sha256(seed)}",
        "business_start": HISTORY_START.isoformat(),
        "business_end": business_end.isoformat(),
        "authoritative": False,
        "simulation_orders_allowed": False,
        "primary_row_count": len(primary_rows),
        "verification_row_count": len(verification_rows),
        "primary_sha256": sha256(primary_rows),
        "verification_sha256": sha256(verification_rows),
        "quality_sha256": sha256(quality_rows),
        "calendar_metadata": {
            "basis": "four-way CSI300/CSI500 official/Tencent date intersection",
            "session_count": len(expected_sessions),
            "sessions_sha256": sha256([value.isoformat() for value in sorted(expected_sessions)]),
            "excluded_known_non_session_rows": [value.isoformat() for value in sorted(disagreements)],
        },
        "accepted": accepted(gates),
        "gates": quality_rows,
    }
    if not manifest["accepted"]:
        raise RuntimeError(f"index critical quality gate failed: {[g.name for g in gates if g.critical and not g.passed]}")
    connection = connect(TiDBConfig.from_env())
    try:
        ensure_index_schema(connection)
        result = publish_index_run(connection, manifest=manifest, primary=primary, verification=verification)
    finally:
        connection.close()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps({"event": "index_finalized", **result}, ensure_ascii=False, sort_keys=True), flush=True)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--business-end", default="")
    parser.add_argument("--output-dir", type=Path, default=Path("index-acceptance"))
    args = parser.parse_args()
    end = date.fromisoformat(args.business_end) if args.business_end else datetime.now(SHANGHAI).date() - timedelta(days=1)
    run(business_end=end, output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

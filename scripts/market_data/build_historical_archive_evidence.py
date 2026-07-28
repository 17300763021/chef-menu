"""Build the bounded, dual-source M2.3 historical archive evidence file."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any

from scripts.market_data.adjustment_engine import build_adjusted_series_from_factor_events
from scripts.market_data.manifest import sha256
from scripts.market_data.sources.akshare_history_source import AkshareEastmoneyHistorySource
from scripts.market_data.sources.baostock_history_source import BaostockHistorySource
from scripts.market_data.sources.frozen_archive_history_source import (
    ARCHIVE_BUSINESS_END,
    ARCHIVE_HISTORY_START,
    ARCHIVE_PATH,
    ARCHIVE_SCHEMA_VERSION,
    ARCHIVE_SYMBOLS,
    FACTOR_SOURCE,
    PRIMARY_SOURCE,
    VERIFICATION_SOURCE,
    frozen_primary_bar,
    frozen_verification_bar,
    validate_archive_document,
)
from scripts.market_data.sources.tencent_history_source import TencentHistorySource


LIFECYCLE_EVIDENCE = {
    "000939": {
        "status": "terminated",
        "official_url": "https://www.szse.cn/disclosure/notice/t20201028_582723.html",
    },
    "002005": {
        "status": "listed_st_as_of_archive_end",
        "official_url": "https://static.cninfo.com.cn/finalpage/2026-04-28/1225215022.PDF",
    },
    "600485": {
        "status": "terminated",
        "official_url": "https://static.sse.com.cn/disclosure/bond/announcement/company/c/new/2026-04-08/136610_20260408_CZ2P.pdf",
    },
}


def _status_rows(rows: dict[date, dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "business_date": business_date.isoformat(),
            "tradestatus": str(row.get("tradestatus", "")),
            "isST": str(row.get("isST", "")),
            "preclose": str(row.get("preclose", "")),
        }
        for business_date, row in sorted(rows.items())
    ]


def _without_hash(value: dict[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def build_document(captured_at: str) -> dict[str, Any]:
    if not captured_at.endswith("Z"):
        raise ValueError("captured_at must be an explicit UTC timestamp ending in Z")
    primary_source = TencentHistorySource(timeout_seconds=20, attempts=2)
    factor_source = AkshareEastmoneyHistorySource(timeout_seconds=20, attempts=2)
    symbols: dict[str, Any] = {}
    with BaostockHistorySource(attempts=2, timeout_seconds=30) as verification_source:
        for symbol in sorted(ARCHIVE_SYMBOLS):
            primary_rows = [
                frozen_primary_bar(row)
                for row in primary_source.fetch_raw(symbol, ARCHIVE_HISTORY_START, ARCHIVE_BUSINESS_END)
            ]
            status = verification_source.fetch_status(symbol, ARCHIVE_HISTORY_START, ARCHIVE_BUSINESS_END)
            verification_rows = [
                frozen_verification_bar(row)
                for row in verification_source.bars_from_status(symbol, status).values()
            ]
            vendor_events = factor_source.fetch_sina_adjustments(symbol, ARCHIVE_BUSINESS_END)
            raw = {row.business_date: row for row in primary_rows}
            _qfq, _hfq, adjustment_events = build_adjusted_series_from_factor_events(
                symbol,
                raw,
                vendor_events,
                source=FACTOR_SOURCE,
            )
            reference = replace(
                verification_source.fetch_reference(symbol),
                name=symbol,
                source=VERIFICATION_SOURCE,
            )
            payload: dict[str, Any] = {
                "primary_source": PRIMARY_SOURCE,
                "verification_source": VERIFICATION_SOURCE,
                "factor_source": FACTOR_SOURCE,
                "lifecycle_evidence": LIFECYCLE_EVIDENCE[symbol],
                "primary_rows": [row.canonical() for row in sorted(primary_rows, key=lambda value: value.business_date)],
                "verification_rows": [
                    row.canonical() for row in sorted(verification_rows, key=lambda value: value.business_date)
                ],
                "status_rows": _status_rows(status),
                "adjustment_events": [row.canonical() for row in adjustment_events],
                "reference": reference.canonical(),
            }
            payload["content_sha256"] = sha256(payload)
            symbols[symbol] = payload

    document: dict[str, Any] = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "authoritative": False,
        "simulation_orders_allowed": False,
        "history_start": ARCHIVE_HISTORY_START.isoformat(),
        "business_end": ARCHIVE_BUSINESS_END.isoformat(),
        "captured_at": captured_at,
        "capture_boundary": "bounded historical repair evidence; never valid for later daily increments",
        "source_roles": {
            "primary": PRIMARY_SOURCE,
            "verification": VERIFICATION_SOURCE,
            "factor": FACTOR_SOURCE,
        },
        "source_versions": {
            "akshare": version("akshare"),
            "baostock": version("baostock"),
        },
        "symbols": symbols,
    }
    document["dataset_sha256"] = sha256(document)
    validate_archive_document(document)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captured-at", required=True, help="fixed UTC timestamp, for example 2026-07-28T08:00:00Z")
    parser.add_argument("--output", type=Path, default=ARCHIVE_PATH)
    args = parser.parse_args()
    document = build_document(args.captured_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    reread = json.loads(args.output.read_text(encoding="utf-8"))
    validate_archive_document(reread)
    print(json.dumps({
        "output": str(args.output),
        "dataset_sha256": reread["dataset_sha256"],
        "symbols": {
            symbol: {
                "primary_rows": len(payload["primary_rows"]),
                "verification_rows": len(payload["verification_rows"]),
                "status_rows": len(payload["status_rows"]),
                "adjustment_events": len(payload["adjustment_events"]),
            }
            for symbol, payload in sorted(reread["symbols"].items())
        },
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

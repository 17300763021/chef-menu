"""Run the bounded M2.1 live source-admission sample."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

from scripts.market_data.contracts import DailyBar, canonical_rows
from scripts.market_data.manifest import build_manifest
from scripts.market_data.quality_gates import accepted, evaluate_source
from scripts.market_data.sources.akshare_source import AkshareSource
from scripts.market_data.sources.baostock_source import BaostockSource


SAMPLE_SYMBOLS = [
    "000001", "000333", "000651", "000858", "002230",
    "002415", "002594", "002714", "300059", "300124",
    "300750", "300760", "600036", "600519", "600900",
    "601012", "601166", "601318", "603259", "688981",
]


def last_n_rows(rows: list[DailyBar], symbols: list[str], count: int) -> list[DailyBar]:
    result: list[DailyBar] = []
    for symbol in symbols:
        available = sorted((row for row in rows if row.symbol == symbol), key=lambda row: row.business_date)
        result.extend(available[-count:])
    return result


def fetch_primary(symbols: list[str], start: date, end: date, workers: int) -> tuple[list[DailyBar], dict[str, str]]:
    rows: list[DailyBar] = []
    failures: dict[str, str] = {}
    source = AkshareSource()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(source.fetch, symbol, start, end): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                rows.extend(future.result())
            except Exception as error:
                failures[symbol] = f"{type(error).__name__}: {error}"
    return rows, failures


def fetch_secondary(symbols: list[str], start: date, end: date) -> tuple[list[DailyBar], dict[str, str]]:
    rows: list[DailyBar] = []
    failures: dict[str, str] = {}
    with BaostockSource() as source:
        for symbol in symbols:
            try:
                rows.extend(source.fetch(symbol, start, end))
            except Exception as error:
                failures[symbol] = f"{type(error).__name__}: {error}"
    return rows, failures


def run(end: date, calendar_days: int = 120, expected_days: int = 60, workers: int = 4) -> tuple[dict[str, Any], list[DailyBar]]:
    start = end - timedelta(days=calendar_days)
    primary, primary_failures = fetch_primary(SAMPLE_SYMBOLS, start, end, workers)
    secondary, secondary_failures = fetch_secondary(SAMPLE_SYMBOLS, start, end)
    primary = last_n_rows([row for row in primary if row.business_date <= end], SAMPLE_SYMBOLS, expected_days)
    secondary = last_n_rows([row for row in secondary if row.business_date <= end], SAMPLE_SYMBOLS, expected_days)
    gates = evaluate_source(primary, secondary, SAMPLE_SYMBOLS, expected_days=expected_days)
    sample = {
        "symbols": SAMPLE_SYMBOLS,
        "symbol_count": len(SAMPLE_SYMBOLS),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "expected_days_per_symbol": expected_days,
        "source_versions": {"akshare": version("akshare"), "baostock": version("baostock")},
        "primary_failures": dict(sorted(primary_failures.items())),
        "secondary_failures": dict(sorted(secondary_failures.items())),
    }
    return build_manifest([*primary, *secondary], gates, sample), [*primary, *secondary]


def write_outputs(output_dir: Path, manifest: dict[str, Any], rows: list[DailyBar]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(canonical_rows(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with (output_dir / "normalized-bars.json.gz").open("wb") as raw_stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_stream, mtime=0) as stream:
            stream.write(payload)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="M2.1 A-share source qualification")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--output-dir", type=Path, default=Path("source-acceptance"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    manifest, rows = run(args.end_date, workers=max(1, min(args.workers, 4)))
    write_outputs(args.output_dir, manifest, rows)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["accepted"] else 2


if __name__ == "__main__":
    sys.exit(main())

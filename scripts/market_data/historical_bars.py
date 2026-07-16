"""Build M2.3 point-in-time historical price and tradeability evidence."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

from scripts.market_data.adjustment_engine import AdjustmentTimeline
from scripts.market_data.contracts import DailyBar
from scripts.market_data.historical_contracts import AdjustmentEvent, HistoricalBar, SecurityReference
from scripts.market_data.historical_quality_gates import evaluate_historical
from scripts.market_data.manifest import sha256
from scripts.market_data.pit_quality_gates import evaluate_calendars, evaluate_universe
from scripts.market_data.pit_universe import HISTORY_START, reconstruct
from scripts.market_data.quality_gates import accepted
from scripts.market_data.sample_capture import SAMPLE_SYMBOLS
from scripts.market_data.sources.akshare_calendar_source import AkshareCalendarSource
from scripts.market_data.sources.akshare_history_source import AkshareHistorySource
from scripts.market_data.sources.baostock_calendar_source import BaostockCalendarSource
from scripts.market_data.sources.baostock_history_source import BaostockHistorySource
from scripts.market_data.sources.csi_index_source import CsiIndexSource
from scripts.market_data.tradeability import derive_tradeability
from scripts.market_data.tradeability_contracts import TradeabilityFact


def membership_keys(sessions: tuple[date, ...], snapshots: dict[date, dict[str, tuple[str, ...]]]) -> dict[tuple[str, date], str]:
    effective_dates = sorted(snapshots)
    output: dict[tuple[str, date], str] = {}
    for session in sessions:
        position = bisect_right(effective_dates, session) - 1
        if position < 0:
            continue
        members = snapshots[effective_dates[position]]
        for index_code, symbols in members.items():
            for symbol in symbols:
                output[(symbol, session)] = index_code
    return output


def fetch_primary(symbols: list[str], ranges: dict[str, tuple[date, date]], workers: int) -> tuple[dict[str, list[DailyBar]], dict[str, str]]:
    source = AkshareHistorySource()
    output: dict[str, list[DailyBar]] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(source.fetch_raw, symbol, *ranges[symbol]): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                output[symbol] = future.result()
            except Exception as error:
                failures[symbol] = f"{type(error).__name__}: {error}"
    return output, failures


def verification_symbols(symbols: list[str], mode: str, maximum: int = 40) -> list[str]:
    """Pre-register a bounded, deterministic cross-vendor verification sample."""
    if mode == "sample" or len(symbols) <= maximum:
        return symbols
    positions = {round(index * (len(symbols) - 1) / (maximum - 1)) for index in range(maximum)}
    return [symbols[index] for index in sorted(positions)]


def listing_age(reference: SecurityReference, session: date, calendar_dates: tuple[date, ...]) -> int:
    return max(0, bisect_right(calendar_dates, session) - bisect_right(calendar_dates, reference.ipo_date - timedelta(days=1)))


def _row_for_tradeability(bar: DailyBar | None) -> dict[str, object] | None:
    if bar is None:
        return None
    return {"high": bar.high, "low": bar.low, "close": bar.close}


def run(end: date, *, mode: str = "sample", workers: int = 4) -> tuple[dict[str, Any], list[HistoricalBar], list[TradeabilityFact], list[AdjustmentEvent], list[SecurityReference]]:
    start = HISTORY_START if mode == "full" else max(HISTORY_START, end - timedelta(days=150))
    primary_calendar = AkshareCalendarSource().fetch(HISTORY_START, end)
    secondary_calendar = BaostockCalendarSource().fetch(HISTORY_START, end)
    calendar_gates = evaluate_calendars(primary_calendar, secondary_calendar)
    csi = CsiIndexSource()
    current = csi.fetch_current()
    if current.as_of_date > end:
        raise ValueError(f"requested end {end} precedes CSI snapshot {current.as_of_date}")
    events, discovered = csi.fetch_events(primary_calendar, current.as_of_date)
    snapshots = reconstruct(current, events)
    universe_gates = evaluate_universe(events, snapshots, discovered, current.as_of_date)
    if not accepted([*calendar_gates, *universe_gates]):
        raise RuntimeError("M2.2 prerequisite gates failed")
    sessions = tuple(value for value in primary_calendar.open_dates if start <= value <= current.as_of_date)
    expected = membership_keys(sessions, snapshots)
    if mode == "sample":
        expected = {key: value for key, value in expected.items() if key[0] in SAMPLE_SYMBOLS}
    symbols = sorted({symbol for symbol, _ in expected})
    ranges = {
        symbol: (min(day for code, day in expected if code == symbol), max(day for code, day in expected if code == symbol))
        for symbol in symbols
    }
    verification_targets = verification_symbols(symbols, mode)
    verification_by_symbol, verification_failures = fetch_primary(verification_targets, ranges, 1)

    secondary_by_symbol: dict[str, dict[date, dict[str, str]]] = {}
    raw_by_symbol: dict[str, dict[date, DailyBar]] = {}
    qfq_by_symbol: dict[str, dict[date, DailyBar]] = {}
    hfq_by_symbol: dict[str, dict[date, DailyBar]] = {}
    references: list[SecurityReference] = []
    adjustments: list[AdjustmentEvent] = []
    secondary_failures: dict[str, str] = {}
    with BaostockHistorySource() as source:
        for symbol in symbols:
            try:
                secondary_by_symbol[symbol] = source.fetch_status(symbol, *ranges[symbol])
                raw_by_symbol[symbol] = source.bars_from_status(symbol, secondary_by_symbol[symbol])
                qfq_by_symbol[symbol] = source.fetch_bars(symbol, *ranges[symbol], "2")
                hfq_by_symbol[symbol] = source.fetch_bars(symbol, *ranges[symbol], "1")
                references.append(source.fetch_reference(symbol))
                adjustments.extend(source.fetch_adjustments(symbol, end))
            except Exception as error:
                secondary_failures[symbol] = f"{type(error).__name__}: {error}"

    primary_map = {(symbol, day): row for symbol, rows in raw_by_symbol.items() for day, row in rows.items() if (symbol, day) in expected}
    verification_map = {row.key: row for rows in verification_by_symbol.values() for row in rows if row.key in expected}
    reference_map = {row.symbol: row for row in references}
    adjustment_map: dict[str, list[AdjustmentEvent]] = {symbol: [] for symbol in symbols}
    for event in adjustments:
        adjustment_map.setdefault(event.symbol, []).append(event)
    timeline_map = {symbol: AdjustmentTimeline(adjustment_map.get(symbol, [])) for symbol in symbols}

    bars: list[HistoricalBar] = []
    facts: list[TradeabilityFact] = []
    close_checks: list[tuple[str, date, Decimal, Decimal]] = []
    for (symbol, business_date), index_code in sorted(expected.items()):
        raw = primary_map.get((symbol, business_date))
        secondary = secondary_by_symbol.get(symbol, {}).get(business_date)
        reference = reference_map.get(symbol)
        age = listing_age(reference, business_date, primary_calendar.open_dates) if reference else 0
        fact = derive_tradeability(
            symbol=symbol, business_date=business_date, index_code=index_code, listing_age_sessions=age,
            primary=_row_for_tradeability(raw), secondary=secondary,
        )
        facts.append(fact)
        if raw is None:
            continue
        qfq_factor, hfq_factor = timeline_map[symbol].factors_on(business_date)
        qfq = qfq_by_symbol.get(symbol, {}).get(business_date)
        hfq = hfq_by_symbol.get(symbol, {}).get(business_date)
        if qfq is None or hfq is None:
            continue
        previous_close = Decimal(secondary["preclose"]) if secondary and secondary.get("preclose") else None
        bars.append(HistoricalBar.build(
            symbol=symbol, business_date=business_date, index_code=index_code,
            open_price=raw.open, high=raw.high, low=raw.low, close=raw.close, previous_close=previous_close,
            volume_shares=raw.volume_shares, amount_cny=raw.amount_cny, turnover_percent=raw.turnover_percent,
            qfq_factor=qfq_factor, hfq_factor=hfq_factor,
            qfq_prices=(qfq.open, qfq.high, qfq.low, qfq.close),
            hfq_prices=(hfq.open, hfq.high, hfq.low, hfq.close), primary_source="baostock",
        ))
        verification = verification_map.get((symbol, business_date))
        if verification:
            close_checks.append((symbol, business_date, raw.close, verification.close))

    verification_expected = sum(
        1 for key in primary_map
        if key[0] in set(verification_targets)
    )

    historical_gates = evaluate_historical(
        expected_keys=set(expected), calendar_dates=set(sessions), bars=bars, facts=facts,
        adjustments=adjustments, close_checks=close_checks, verification_expected=verification_expected,
    )
    gates = [*calendar_gates, *universe_gates, *historical_gates]
    canonical_bars = [row.canonical() for row in sorted(bars, key=lambda value: value.key)]
    canonical_facts = [row.canonical() for row in sorted(facts, key=lambda value: (value.symbol, value.business_date))]
    canonical_adjustments = [row.canonical() for row in sorted(adjustments, key=lambda value: (value.symbol, value.effective_date))]
    manifest = {
        "manifest_version": "m2-historical-market-manifest-v1", "authoritative": False,
        "simulation_orders_allowed": False, "mode": mode, "history_start": start.isoformat(),
        "business_end": current.as_of_date.isoformat(), "symbol_count": len(symbols),
        "verification_symbol_count": len(verification_targets),
        "expected_key_count": len(expected), "bar_count": len(bars), "tradeability_count": len(facts),
        "adjustment_event_count": len(adjustments), "verification_source": "akshare_sina",
        "verification_failures": dict(sorted(verification_failures.items())),
        "primary_failures": dict(sorted(secondary_failures.items())),
        "source_versions": {"akshare": version("akshare"), "baostock": version("baostock")},
        "bars_sha256": sha256(canonical_bars), "tradeability_sha256": sha256(canonical_facts),
        "adjustments_sha256": sha256(canonical_adjustments), "accepted": accepted(gates),
        "gates": [gate.canonical() for gate in gates],
    }
    return manifest, bars, facts, adjustments, references


def _write_gzip(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(payload)


def write_outputs(output_dir: Path, manifest: dict[str, Any], bars: list[HistoricalBar], facts: list[TradeabilityFact], adjustments: list[AdjustmentEvent], references: list[SecurityReference]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_gzip(output_dir / "historical-bars.json.gz", [row.canonical() for row in sorted(bars, key=lambda value: value.key)])
    _write_gzip(output_dir / "tradeability.json.gz", [row.canonical() for row in sorted(facts, key=lambda value: (value.symbol, value.business_date))])
    _write_gzip(output_dir / "adjustment-events.json.gz", [row.canonical() for row in sorted(adjustments, key=lambda value: (value.symbol, value.effective_date))])
    (output_dir / "security-references.json").write_text(json.dumps([row.canonical() for row in sorted(references, key=lambda value: value.symbol)], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="M2.3 historical market-data acceptance")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--mode", choices=("sample", "full"), default="sample")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, default=Path("historical-market-acceptance"))
    args = parser.parse_args()
    result = run(args.end_date, mode=args.mode, workers=args.workers)
    manifest = result[0]
    write_outputs(args.output_dir, *result)
    print(json.dumps({key: manifest[key] for key in ("accepted", "mode", "business_end", "symbol_count", "expected_key_count", "bar_count", "tradeability_count", "adjustment_event_count", "bars_sha256", "tradeability_sha256", "adjustments_sha256", "gates")}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["accepted"] else 2


if __name__ == "__main__":
    sys.exit(main())

"""Build M2.3 point-in-time historical price and tradeability evidence."""

from __future__ import annotations

import argparse
import gzip
import json
import signal
import sys
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
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


SHARD_SIZE = 10
PREFLIGHT_SYMBOLS = 100
SYMBOL_DEADLINE_SECONDS = 90


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
    source = AkshareHistorySource(attempts=5)
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


def bounded_symbols(symbols: list[str], maximum: int | None) -> list[str]:
    if maximum is None or len(symbols) <= maximum:
        return symbols
    if maximum < 2:
        raise ValueError("symbol limit must be at least 2")
    positions = {round(index * (len(symbols) - 1) / (maximum - 1)) for index in range(maximum)}
    return [symbols[index] for index in sorted(positions)]


def shard_symbols(symbols: list[str], shard_index: int, shard_count: int) -> list[str]:
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard coordinates")
    return symbols[shard_index::shard_count]


def _progress(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False, sort_keys=True), flush=True)


@contextmanager
def symbol_deadline(seconds: int):
    """Enforce a real per-symbol deadline on the Linux cloud runner."""
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def raise_timeout(signum: int, frame: object) -> None:
        raise TimeoutError(f"symbol acquisition exceeded {seconds} seconds")

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def listing_age(reference: SecurityReference, session: date, calendar_dates: tuple[date, ...]) -> int:
    return max(0, bisect_right(calendar_dates, session) - bisect_right(calendar_dates, reference.ipo_date - timedelta(days=1)))


def _row_for_tradeability(bar: DailyBar | None) -> dict[str, object] | None:
    if bar is None:
        return None
    return {"high": bar.high, "low": bar.low, "close": bar.close}


def run(
    end: date,
    *,
    mode: str = "sample",
    workers: int = 4,
    shard_index: int = 0,
    shard_count: int = 1,
    symbol_attempts: int = 2,
) -> tuple[dict[str, Any], list[HistoricalBar], list[TradeabilityFact], list[AdjustmentEvent], list[SecurityReference]]:
    start = HISTORY_START if mode in {"preflight", "full"} else max(HISTORY_START, end - timedelta(days=150))
    _progress("prerequisites_started", end_date=end.isoformat(), mode=mode)
    primary_calendar = AkshareCalendarSource().fetch(HISTORY_START, end)
    secondary_calendar = BaostockCalendarSource().fetch(HISTORY_START, end)
    _progress("calendars_loaded", primary_sessions=len(primary_calendar.open_dates), secondary_sessions=len(secondary_calendar.open_dates))
    calendar_gates = evaluate_calendars(primary_calendar, secondary_calendar)
    csi = CsiIndexSource()
    current = csi.fetch_current()
    if current.as_of_date > end:
        raise ValueError(f"requested end {end} precedes CSI snapshot {current.as_of_date}")
    events, discovered = csi.fetch_events(primary_calendar, current.as_of_date)
    snapshots = reconstruct(current, events)
    _progress("universe_reconstructed", effective_snapshots=len(snapshots), official_events=len(events), as_of_date=current.as_of_date.isoformat())
    universe_gates = evaluate_universe(events, snapshots, discovered, current.as_of_date)
    if not accepted([*calendar_gates, *universe_gates]):
        raise RuntimeError("M2.2 prerequisite gates failed")
    sessions = tuple(value for value in primary_calendar.open_dates if start <= value <= current.as_of_date)
    expected = membership_keys(sessions, snapshots)
    if mode == "sample":
        expected = {key: value for key, value in expected.items() if key[0] in SAMPLE_SYMBOLS}
    all_symbols = sorted({symbol for symbol, _ in expected})
    selected_symbols = bounded_symbols(all_symbols, PREFLIGHT_SYMBOLS if mode == "preflight" else None)
    selected_set = set(selected_symbols)
    expected = {key: value for key, value in expected.items() if key[0] in selected_set}
    global_expected_key_count = len(expected)
    global_verification_targets = verification_symbols(selected_symbols, "sample" if mode == "sample" else "full")
    symbols = shard_symbols(selected_symbols, shard_index, shard_count)
    symbol_set = set(symbols)
    expected = {key: value for key, value in expected.items() if key[0] in symbol_set}
    _progress(
        "scope_ready", mode=mode, shard_index=shard_index, shard_count=shard_count,
        shard_symbols=len(symbols), global_symbols=len(selected_symbols), expected_keys=len(expected),
    )
    ranges = {
        symbol: (min(day for code, day in expected if code == symbol), max(day for code, day in expected if code == symbol))
        for symbol in symbols
    }
    verification_targets = [symbol for symbol in global_verification_targets if symbol in symbol_set]
    verification_delay = 0 if mode == "sample" else (shard_index % 4) * 5
    if verification_delay:
        _progress("verification_stagger", delay_seconds=verification_delay)
        time.sleep(verification_delay)
    _progress("verification_started", symbols=len(verification_targets))
    verification_by_symbol, verification_failures = fetch_primary(verification_targets, ranges, 1)
    _progress("verification_completed", succeeded=len(verification_by_symbol), failed=len(verification_failures))

    secondary_by_symbol: dict[str, dict[date, dict[str, str]]] = {}
    raw_by_symbol: dict[str, dict[date, DailyBar]] = {}
    qfq_by_symbol: dict[str, dict[date, tuple[Decimal, Decimal, Decimal, Decimal]]] = {}
    hfq_by_symbol: dict[str, dict[date, tuple[Decimal, Decimal, Decimal, Decimal]]] = {}
    references: list[SecurityReference] = []
    adjustments: list[AdjustmentEvent] = []
    secondary_failures: dict[str, str] = {}
    started_at = time.monotonic()
    with BaostockHistorySource(timeout_seconds=30) as source:
        for position, symbol in enumerate(symbols, start=1):
            last_error: Exception | None = None
            for attempt in range(1, symbol_attempts + 1):
                try:
                    with symbol_deadline(SYMBOL_DEADLINE_SECONDS):
                        status = source.fetch_status(symbol, *ranges[symbol])
                        raw = source.bars_from_status(symbol, status)
                        qfq = source.fetch_adjusted_prices(symbol, *ranges[symbol], "2")
                        hfq = source.fetch_adjusted_prices(symbol, *ranges[symbol], "1")
                        reference = source.fetch_reference(symbol)
                        events_for_symbol = source.fetch_adjustments(symbol, end)
                    secondary_by_symbol[symbol] = status
                    raw_by_symbol[symbol] = raw
                    qfq_by_symbol[symbol] = qfq
                    hfq_by_symbol[symbol] = hfq
                    references.append(reference)
                    adjustments.extend(events_for_symbol)
                    last_error = None
                    break
                except Exception as error:
                    last_error = error
                    _progress("symbol_retry", symbol=symbol, attempt=attempt, error=f"{type(error).__name__}: {error}")
                    if attempt < symbol_attempts:
                        time.sleep(2 ** (attempt - 1))
            if last_error is not None:
                secondary_failures[symbol] = f"{type(last_error).__name__}: {last_error}"
            elapsed = max(time.monotonic() - started_at, 0.001)
            _progress(
                "symbol_completed", symbol=symbol, completed=position, total=len(symbols),
                succeeded=position - len(secondary_failures), failed=len(secondary_failures),
                elapsed_seconds=round(elapsed, 1), estimated_remaining_seconds=round(elapsed / position * (len(symbols) - position), 1),
            )

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
            qfq_prices=qfq, hfq_prices=hfq, primary_source="baostock",
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
        "global_symbol_count": len(selected_symbols), "global_expected_key_count": global_expected_key_count,
        "shard_index": shard_index, "shard_count": shard_count,
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


def build_plan(end: date, mode: str) -> dict[str, Any]:
    if mode == "sample":
        shard_count = 1
        symbol_limit = len(SAMPLE_SYMBOLS)
    else:
        primary_calendar = AkshareCalendarSource().fetch(HISTORY_START, end)
        csi = CsiIndexSource()
        current = csi.fetch_current()
        events, _ = csi.fetch_events(primary_calendar, current.as_of_date)
        snapshots = reconstruct(current, events)
        sessions = tuple(value for value in primary_calendar.open_dates if HISTORY_START <= value <= current.as_of_date)
        symbols = sorted({symbol for symbol, _ in membership_keys(sessions, snapshots)})
        symbol_limit = min(len(symbols), PREFLIGHT_SYMBOLS) if mode == "preflight" else len(symbols)
        shard_count = (symbol_limit + SHARD_SIZE - 1) // SHARD_SIZE
    return {
        "mode": mode, "business_end": end.isoformat(), "symbol_count": symbol_limit,
        "shard_size": SHARD_SIZE, "shard_count": shard_count,
        "matrix": {"include": [{"shard_index": index, "shard_count": shard_count} for index in range(shard_count)]},
    }


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
    parser.add_argument("--mode", choices=("sample", "preflight", "full"), default="sample")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--plan-output", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("historical-market-acceptance"))
    args = parser.parse_args()
    if args.plan_output:
        plan = build_plan(args.end_date, args.mode)
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    result = run(
        args.end_date, mode=args.mode, workers=args.workers,
        shard_index=args.shard_index, shard_count=args.shard_count,
    )
    manifest = result[0]
    write_outputs(args.output_dir, *result)
    print(json.dumps({key: manifest[key] for key in ("accepted", "mode", "business_end", "symbol_count", "expected_key_count", "bar_count", "tradeability_count", "adjustment_event_count", "bars_sha256", "tradeability_sha256", "adjustments_sha256", "gates")}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["accepted"] else 2


if __name__ == "__main__":
    sys.exit(main())

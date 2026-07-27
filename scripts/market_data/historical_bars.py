"""Build M2.3 point-in-time historical price and tradeability evidence."""

from __future__ import annotations

import argparse
import gzip
import json
import os
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
from typing import Any, Callable

from scripts.market_data.calendar_contracts import CALENDAR_SCHEMA_VERSION, TradingCalendar
from scripts.market_data.contracts import DailyBar
from scripts.market_data.historical_contracts import AdjustmentEvent, HistoricalBar, SecurityReference
from scripts.market_data.historical_quality_gates import evaluate_historical
from scripts.market_data.manifest import sha256
from scripts.market_data.pit_quality_gates import evaluate_calendars, evaluate_universe
from scripts.market_data.pit_universe import HISTORY_START, reconstruct
from scripts.market_data.quality_gates import GateResult, accepted
from scripts.market_data.sample_capture import SAMPLE_SYMBOLS
from scripts.market_data.sources.akshare_calendar_source import AkshareCalendarSource
from scripts.market_data.sources.akshare_history_source import AkshareEastmoneyHistorySource, AkshareHistorySource
from scripts.market_data.sources.baostock_calendar_source import BaostockCalendarSource
from scripts.market_data.sources.csi_index_source import CsiIndexSource
from scripts.market_data.tradeability import derive_tradeability
from scripts.market_data.tradeability_contracts import TradeabilityFact
from scripts.market_data.universe_contracts import CurrentUniverse, INDEX_SIZES


SHARD_SIZE = 10
SMOKE_SYMBOLS = 20
PREFLIGHT_SYMBOLS = 100
SYMBOL_DEADLINE_SECONDS = 60
FULL_HISTORY_ACQUISITION_STAGGER_SECONDS = 10
FULL_HISTORY_ACQUISITION_STAGGER_BUCKETS = 6


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
    source = AkshareHistorySource(timeout_seconds=15, attempts=2)
    output: dict[str, list[DailyBar]] = {}
    failures: dict[str, str] = {}
    if workers == 1:
        for symbol in symbols:
            try:
                with symbol_deadline(SYMBOL_DEADLINE_SECONDS):
                    output[symbol] = source.fetch_raw(symbol, *ranges[symbol])
            except Exception as error:
                failures[symbol] = f"{type(error).__name__}: {error}"
        return output, failures
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


def history_stagger_seconds(mode: str, shard_index: int) -> int:
    if mode != "full":
        return 0
    return (shard_index % FULL_HISTORY_ACQUISITION_STAGGER_BUCKETS) * FULL_HISTORY_ACQUISITION_STAGGER_SECONDS


def current_universe_from_canonical(value: dict[str, Any]) -> CurrentUniverse:
    if value.get("schema_version") != "m2-csi800-pit-universe-v1":
        raise ValueError("unsupported current universe schema")
    members = {
        index_code: tuple(sorted(normalized for normalized in (str(item).zfill(6) for item in value["members"][index_code])))
        for index_code in INDEX_SIZES
    }
    return CurrentUniverse(
        as_of_date=date.fromisoformat(str(value["as_of_date"])),
        members=members,
        source_urls={str(key): str(item) for key, item in value["source_urls"].items()},
        source_hashes={str(key): str(item) for key, item in value["source_hashes"].items()},
    )


def trading_calendar_from_canonical(value: dict[str, Any]) -> TradingCalendar:
    if value.get("schema_version") != CALENDAR_SCHEMA_VERSION:
        raise ValueError("unsupported trading calendar schema")
    return TradingCalendar.build(
        source=str(value["source"]),
        start_date=date.fromisoformat(str(value["start_date"])),
        end_date=date.fromisoformat(str(value["end_date"])),
        values=(date.fromisoformat(str(item)) for item in value["open_dates"]),
    )


def load_calendars(
    end: date,
    *,
    primary_calendar: TradingCalendar | None = None,
    secondary_calendar: TradingCalendar | None = None,
) -> tuple[TradingCalendar, TradingCalendar, list[GateResult], dict[str, str]]:
    primary = primary_calendar or AkshareCalendarSource().fetch(HISTORY_START, end)
    secondary = secondary_calendar or BaostockCalendarSource().fetch(HISTORY_START, end)
    calendar_gates = evaluate_calendars(primary, secondary)
    calendar_source = {
        "primary_calendar_sha256": sha256(primary.canonical()),
        "secondary_calendar_sha256": sha256(secondary.canonical()),
    }
    return primary, secondary, calendar_gates, calendar_source


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


def _historical_bar_from_canonical(row: dict[str, Any]) -> HistoricalBar:
    decimal_fields = (
        "open", "high", "low", "close", "previous_close", "amount_cny", "turnover_percent",
        "qfq_factor", "hfq_factor", "qfq_open", "qfq_high", "qfq_low", "qfq_close",
        "hfq_open", "hfq_high", "hfq_low", "hfq_close",
    )
    values = {field: None if row.get(field) is None else Decimal(str(row[field])) for field in decimal_fields}
    return HistoricalBar(
        symbol=str(row["symbol"]), exchange=str(row["exchange"]),
        business_date=date.fromisoformat(str(row["business_date"])), index_code=str(row["index_code"]),
        open=values["open"], high=values["high"], low=values["low"], close=values["close"],
        previous_close=values["previous_close"], volume_shares=int(row["volume_shares"]),
        amount_cny=values["amount_cny"], turnover_percent=values["turnover_percent"],
        qfq_factor=values["qfq_factor"], hfq_factor=values["hfq_factor"],
        qfq_open=values["qfq_open"], qfq_high=values["qfq_high"], qfq_low=values["qfq_low"], qfq_close=values["qfq_close"],
        hfq_open=values["hfq_open"], hfq_high=values["hfq_high"], hfq_low=values["hfq_low"], hfq_close=values["hfq_close"],
        primary_source=str(row.get("primary_source") or "unknown"),
        factor_source=str(row.get("factor_source") or "unknown"),
        schema_version=str(row.get("schema_version") or "m2-historical-market-v1"),
    )


def _tradeability_from_canonical(row: dict[str, Any]) -> TradeabilityFact:
    return TradeabilityFact(
        symbol=str(row["symbol"]), business_date=date.fromisoformat(str(row["business_date"])),
        index_code=str(row["index_code"]), has_primary_bar=bool(row["has_primary_bar"]),
        has_secondary_status=bool(row["has_secondary_status"]), is_suspended=bool(row["is_suspended"]),
        is_st=None if row.get("is_st") is None else bool(row["is_st"]),
        listing_age_sessions=int(row["listing_age_sessions"]),
        limit_rate=None if row.get("limit_rate") is None else Decimal(str(row["limit_rate"])),
        limit_up=None if row.get("limit_up") is None else Decimal(str(row["limit_up"])),
        limit_down=None if row.get("limit_down") is None else Decimal(str(row["limit_down"])),
        at_limit_up=bool(row["at_limit_up"]), at_limit_down=bool(row["at_limit_down"]),
        one_price_limit_up=bool(row["one_price_limit_up"]), one_price_limit_down=bool(row["one_price_limit_down"]),
        can_buy=bool(row["can_buy"]), can_sell=bool(row["can_sell"]),
        block_reasons=tuple(str(value) for value in row.get("block_reasons", [])),
        schema_version=str(row.get("schema_version") or "m2-tradeability-v1"),
    )


def _adjustment_from_canonical(row: dict[str, Any]) -> AdjustmentEvent:
    return AdjustmentEvent(
        symbol=str(row["symbol"]), effective_date=date.fromisoformat(str(row["effective_date"])),
        qfq_factor=Decimal(str(row["qfq_factor"])), hfq_factor=Decimal(str(row["hfq_factor"])),
        source=str(row.get("source") or "unknown"),
    )


def _reference_from_canonical(row: dict[str, Any]) -> SecurityReference:
    return SecurityReference(
        symbol=str(row["symbol"]), exchange=str(row["exchange"]), name=str(row["name"]),
        ipo_date=date.fromisoformat(str(row["ipo_date"])),
        out_date=None if row.get("out_date") is None else date.fromisoformat(str(row["out_date"])),
        source=str(row.get("source") or "unknown"),
    )


def _close_check_from_canonical(row: dict[str, Any]) -> tuple[str, date, Decimal, Decimal]:
    return (
        str(row["symbol"]), date.fromisoformat(str(row["business_date"])),
        Decimal(str(row["primary_close"])), Decimal(str(row["verification_close"])),
    )


def run(
    end: date,
    *,
    mode: str = "sample",
    workers: int = 4,
    shard_index: int = 0,
    shard_count: int = 1,
    symbol_attempts: int = 1,
    current_universe: CurrentUniverse | None = None,
    csi_discovered_notice_ids: set[int] | None = None,
    primary_calendar: TradingCalendar | None = None,
    secondary_calendar: TradingCalendar | None = None,
    resume_evidence: Any | None = None,
    checkpoint_callback: Callable[..., None] | None = None,
) -> tuple[dict[str, Any], list[HistoricalBar], list[TradeabilityFact], list[AdjustmentEvent], list[SecurityReference], list[tuple[str, date, Decimal, Decimal]]]:
    start = HISTORY_START if mode in {"smoke", "preflight", "full"} else max(HISTORY_START, end - timedelta(days=150))
    _progress("prerequisites_started", end_date=end.isoformat(), mode=mode)
    using_frozen_calendars = primary_calendar is not None and secondary_calendar is not None
    primary_calendar, secondary_calendar, calendar_gates, calendar_source = load_calendars(
        end, primary_calendar=primary_calendar, secondary_calendar=secondary_calendar,
    )
    _progress(
        "calendars_loaded",
        primary_sessions=len(primary_calendar.open_dates),
        secondary_sessions=len(secondary_calendar.open_dates),
        frozen=using_frozen_calendars,
    )
    csi = CsiIndexSource()
    current = current_universe or csi.fetch_current()
    if current.as_of_date > end:
        raise ValueError(f"requested end {end} precedes CSI snapshot {current.as_of_date}")
    events, discovered, event_index_source = csi.fetch_indexed_events(current.as_of_date, csi_discovered_notice_ids)
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
    symbol_limit = SMOKE_SYMBOLS if mode == "smoke" else PREFLIGHT_SYMBOLS if mode == "preflight" else None
    selected_symbols = bounded_symbols(all_symbols, symbol_limit)
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

    resumed_bars = [_historical_bar_from_canonical(row) for row in getattr(resume_evidence, "bars", [])]
    resumed_facts = [_tradeability_from_canonical(row) for row in getattr(resume_evidence, "tradeability", [])]
    resumed_adjustments = [_adjustment_from_canonical(row) for row in getattr(resume_evidence, "adjustments", [])]
    resumed_references = [_reference_from_canonical(row) for row in getattr(resume_evidence, "references", [])]
    resumed_checks = [_close_check_from_canonical(row) for row in getattr(resume_evidence, "verification_checks", [])]
    claimed_resumed = set(getattr(resume_evidence, "manifest", {}).get("resumed_symbols", []))
    resumed_bar_keys = {(row.symbol, row.business_date) for row in resumed_bars}
    resumed_fact_keys = {(row.symbol, row.business_date) for row in resumed_facts}
    resumed_reference_symbols = {row.symbol for row in resumed_references}
    valid_resumed: set[str] = set()
    for symbol in claimed_resumed & symbol_set:
        expected_for_symbol = {key for key in expected if key[0] == symbol}
        bars_for_symbol = {key for key in resumed_bar_keys if key[0] == symbol}
        facts_for_symbol = {key for key in resumed_fact_keys if key[0] == symbol}
        if bars_for_symbol and bars_for_symbol <= expected_for_symbol and facts_for_symbol == expected_for_symbol and symbol in resumed_reference_symbols:
            valid_resumed.add(symbol)
    ignored_resumed = sorted((claimed_resumed & symbol_set) - valid_resumed)
    if claimed_resumed:
        _progress("tidb_resume_validated", resumed=len(valid_resumed), ignored=len(ignored_resumed), ignored_symbols=ignored_resumed)

    bars = [row for row in resumed_bars if row.symbol in valid_resumed]
    facts = [row for row in resumed_facts if row.symbol in valid_resumed]
    adjustments = [row for row in resumed_adjustments if row.symbol in valid_resumed]
    references = [row for row in resumed_references if row.symbol in valid_resumed]
    close_checks = [row for row in resumed_checks if row[0] in valid_resumed and (row[0], row[1]) in expected]
    primary_sources_by_symbol = {
        symbol: next(row.primary_source for row in bars if row.symbol == symbol)
        for symbol in sorted(valid_resumed)
    }
    resumed_check_counts = {
        symbol: sum(1 for check in close_checks if check[0] == symbol)
        for symbol in verification_targets
    }
    resumed_bar_counts = {
        symbol: sum(1 for row in bars if row.symbol == symbol)
        for symbol in verification_targets
    }
    verification_fetch_targets = [
        symbol for symbol in verification_targets
        if symbol not in valid_resumed or resumed_check_counts[symbol] < resumed_bar_counts[symbol]
    ]
    verification_delay = 0 if mode == "sample" else (shard_index % 4) * 5
    if verification_fetch_targets and verification_delay:
        _progress("verification_stagger", delay_seconds=verification_delay)
        time.sleep(verification_delay)
    _progress("verification_started", symbols=len(verification_fetch_targets), resumed=len(verification_targets) - len(verification_fetch_targets))
    verification_by_symbol, verification_failures = fetch_primary(verification_fetch_targets, ranges, 1) if verification_fetch_targets else ({}, {})
    _progress("verification_completed", succeeded=len(verification_by_symbol), failed=len(verification_failures))
    verification_map = {row.key: row for rows in verification_by_symbol.values() for row in rows if row.key in expected}
    if verification_fetch_targets:
        refreshed_symbols = set(verification_fetch_targets)
        close_checks = [row for row in close_checks if row[0] not in refreshed_symbols]
        for resumed_bar in bars:
            if resumed_bar.symbol not in refreshed_symbols:
                continue
            verification = verification_map.get(resumed_bar.key)
            if verification is not None:
                close_checks.append((
                    resumed_bar.symbol, resumed_bar.business_date,
                    resumed_bar.close, verification.close,
                ))

    acquisition_symbols = [symbol for symbol in symbols if symbol not in valid_resumed]
    history_delay = history_stagger_seconds(mode, shard_index)
    if acquisition_symbols and history_delay:
        _progress("history_stagger", delay_seconds=history_delay)
        time.sleep(history_delay)

    primary_failures: dict[str, str] = {}
    checkpoint_failures: dict[str, str] = {}
    started_at = time.monotonic()
    source = AkshareEastmoneyHistorySource(timeout_seconds=20, attempts=2)
    for position, symbol in enumerate(acquisition_symbols, start=1):
        last_error: Exception | None = None
        raw: dict[date, DailyBar] = {}
        qfq: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        hfq: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        factors: dict[date, tuple[Decimal, Decimal]] = {}
        status: dict[date, dict[str, str]] = {}
        events_for_symbol: list[AdjustmentEvent] = []
        reference: SecurityReference | None = None
        primary_source_name: str | None = None
        for attempt in range(1, symbol_attempts + 1):
            try:
                with symbol_deadline(SYMBOL_DEADLINE_SECONDS):
                    raw, qfq, hfq, events_for_symbol, reference, status, primary_source_name = source.fetch_bundle(symbol, *ranges[symbol])
                    factors = {
                        business_date: (
                            source._factor(qfq[business_date][3], raw[business_date].close),
                            source._factor(hfq[business_date][3], raw[business_date].close),
                        )
                        for business_date in set(raw) & set(qfq) & set(hfq)
                    }
                last_error = None
                break
            except Exception as error:
                last_error = error
                _progress("symbol_retry", symbol=symbol, attempt=attempt, error=f"{type(error).__name__}: {error}")
                if attempt < symbol_attempts:
                    time.sleep(2 ** (attempt - 1))

        symbol_bars: list[HistoricalBar] = []
        symbol_facts: list[TradeabilityFact] = []
        symbol_checks: list[tuple[str, date, Decimal, Decimal]] = []
        for (expected_symbol, business_date), index_code in sorted(expected.items()):
            if expected_symbol != symbol:
                continue
            primary = raw.get(business_date)
            secondary = status.get(business_date)
            age = listing_age(reference, business_date, primary_calendar.open_dates) if reference else 0
            symbol_facts.append(derive_tradeability(
                symbol=symbol, business_date=business_date, index_code=index_code, listing_age_sessions=age,
                primary=_row_for_tradeability(primary), secondary=secondary,
            ))
            symbol_factors = factors.get(business_date)
            adjusted_qfq = qfq.get(business_date)
            adjusted_hfq = hfq.get(business_date)
            if primary is None or symbol_factors is None or adjusted_qfq is None or adjusted_hfq is None:
                continue
            qfq_factor, hfq_factor = symbol_factors
            previous_close = Decimal(secondary["preclose"]) if secondary and secondary.get("preclose") else None
            symbol_bars.append(HistoricalBar.build(
                symbol=symbol, business_date=business_date, index_code=index_code,
                open_price=primary.open, high=primary.high, low=primary.low, close=primary.close,
                previous_close=previous_close, volume_shares=primary.volume_shares,
                amount_cny=primary.amount_cny, turnover_percent=primary.turnover_percent,
                qfq_factor=qfq_factor, hfq_factor=hfq_factor,
                qfq_prices=adjusted_qfq, hfq_prices=adjusted_hfq,
                primary_source=primary_source_name or "unknown",
            ))
            verification = verification_map.get((symbol, business_date))
            if verification:
                symbol_checks.append((symbol, business_date, primary.close, verification.close))

        error_message = None
        if last_error is not None:
            error_message = f"{type(last_error).__name__}: {last_error}"
            primary_failures[symbol] = error_message
        else:
            primary_sources_by_symbol[symbol] = primary_source_name or "unknown"
            if reference is not None:
                references.append(reference)
            adjustments.extend(events_for_symbol)
        bars.extend(symbol_bars)
        facts.extend(symbol_facts)
        close_checks.extend(symbol_checks)

        if checkpoint_callback is not None:
            try:
                checkpoint_callback(
                    symbol, symbol_bars, symbol_facts, events_for_symbol if last_error is None else [],
                    reference if last_error is None else None, symbol_checks,
                    primary_source_name if last_error is None else None, error_message,
                )
                _progress("tidb_symbol_checkpointed", symbol=symbol, status="failed" if error_message else "succeeded")
            except Exception as error:
                checkpoint_failures[symbol] = f"{type(error).__name__}: {error}"
                _progress("tidb_symbol_checkpoint_failed", symbol=symbol, error=checkpoint_failures[symbol])

        elapsed = max(time.monotonic() - started_at, 0.001)
        completed = len(valid_resumed) + position
        total = len(symbols)
        _progress(
            "symbol_completed", symbol=symbol, completed=completed, total=total,
            resumed=len(valid_resumed), succeeded=completed - len(primary_failures), failed=len(primary_failures),
            elapsed_seconds=round(elapsed, 1),
            estimated_remaining_seconds=round(elapsed / position * (len(acquisition_symbols) - position), 1),
        )

    verification_expected = sum(1 for row in bars if row.symbol in set(verification_targets))

    historical_gates = evaluate_historical(
        expected_keys=set(expected), calendar_dates=set(sessions), bars=bars, facts=facts,
        adjustments=adjustments, close_checks=close_checks, verification_expected=verification_expected,
        cross_source_critical=shard_count == 1,
    )
    gates = [*calendar_gates, *universe_gates, *historical_gates]
    if checkpoint_callback is not None:
        gates.append(GateResult(
            "tidb_symbol_checkpoint_writes", not checkpoint_failures, len(checkpoint_failures), "= 0",
            details=tuple(f"{symbol}: {message}" for symbol, message in sorted(checkpoint_failures.items())),
        ))
    canonical_bars = [row.canonical() for row in sorted(bars, key=lambda value: value.key)]
    canonical_facts = [row.canonical() for row in sorted(facts, key=lambda value: (value.symbol, value.business_date))]
    canonical_adjustments = [row.canonical() for row in sorted(adjustments, key=lambda value: (value.symbol, value.effective_date))]
    canonical_close_checks = [
        {
            "symbol": symbol, "business_date": business_date.isoformat(),
            "primary_close": format(primary, "f"), "verification_close": format(verification, "f"),
        }
        for symbol, business_date, primary, verification in sorted(close_checks)
    ]
    verification_sources_by_symbol = {
        symbol: sorted({row.source for row in rows})
        for symbol, rows in sorted(verification_by_symbol.items())
    }
    verification_source_overlap_symbols = sorted(
        symbol for symbol, sources in verification_sources_by_symbol.items()
        if primary_sources_by_symbol.get(symbol) in sources
    )
    manifest = {
        "manifest_version": "m2-historical-market-manifest-v1", "authoritative": False,
        "simulation_orders_allowed": False, "mode": mode, "history_start": start.isoformat(),
        "business_end": current.as_of_date.isoformat(), "symbol_count": len(symbols),
        "global_symbol_count": len(selected_symbols), "global_expected_key_count": global_expected_key_count,
        "shard_index": shard_index, "shard_count": shard_count,
        "global_verification_symbol_count": len(global_verification_targets),
        "global_verification_symbols_sha256": sha256(global_verification_targets),
        "verification_symbol_count": len(verification_targets),
        "verification_symbols": verification_targets,
        "verification_expected_count": verification_expected,
        "verification_check_count": len(close_checks),
        "expected_key_count": len(expected), "bar_count": len(bars), "tradeability_count": len(facts),
        "adjustment_event_count": len(adjustments),
        "primary_source": "akshare_historical_bundle",
        "primary_source_role": "per-symbol single-mouth historical raw/qfq/hfq source",
        "primary_sources_by_symbol": dict(sorted(primary_sources_by_symbol.items())),
        "tradeability_status_source": "akshare_observed_raw_fail_closed",
        "verification_source": "akshare_sina_with_eastmoney_fallback",
        "verification_sources_by_symbol": verification_sources_by_symbol,
        "verification_source_overlap_symbols": verification_source_overlap_symbols,
        "csi_event_index_source": event_index_source,
        "calendar_source": calendar_source,
        "verification_failures": dict(sorted(verification_failures.items())),
        "primary_failures": dict(sorted(primary_failures.items())),
        "checkpoint_failures": dict(sorted(checkpoint_failures.items())),
        "resumed_symbols": sorted(valid_resumed),
        "resumed_symbol_count": len(valid_resumed),
        "acquired_symbol_count": len(acquisition_symbols),
        "source_versions": {"akshare": version("akshare")},
        "bars_sha256": sha256(canonical_bars), "tradeability_sha256": sha256(canonical_facts),
        "adjustments_sha256": sha256(canonical_adjustments),
        "verification_checks_sha256": sha256(canonical_close_checks), "accepted": accepted(gates),
        "gates": [gate.canonical() for gate in gates],
    }
    return manifest, bars, facts, adjustments, references, close_checks


def build_plan(end: date, mode: str) -> dict[str, Any]:
    current_snapshot: dict[str, object] | None = None
    primary_calendar_snapshot: dict[str, object] | None = None
    secondary_calendar_snapshot: dict[str, object] | None = None
    calendar_gates: list[GateResult] = []
    calendar_source: dict[str, str] | None = None
    if mode == "sample":
        shard_count = 1
        symbol_limit = len(SAMPLE_SYMBOLS)
    else:
        primary_calendar, secondary_calendar, calendar_gates, calendar_source = load_calendars(end)
        if not accepted(calendar_gates):
            raise RuntimeError("M2.2 calendar prerequisite gates failed")
        csi = CsiIndexSource()
        current = csi.fetch_current()
        if current.as_of_date > end:
            raise ValueError(f"requested end {end} precedes CSI snapshot {current.as_of_date}")
        events, discovered, event_index_source = csi.fetch_indexed_events(current.as_of_date)
        snapshots = reconstruct(current, events)
        sessions = tuple(value for value in primary_calendar.open_dates if HISTORY_START <= value <= current.as_of_date)
        symbols = sorted({symbol for symbol, _ in membership_keys(sessions, snapshots)})
        requested_limit = SMOKE_SYMBOLS if mode == "smoke" else PREFLIGHT_SYMBOLS if mode == "preflight" else len(symbols)
        symbol_limit = min(len(symbols), requested_limit)
        shard_count = (symbol_limit + SHARD_SIZE - 1) // SHARD_SIZE
        current_snapshot = current.canonical()
        primary_calendar_snapshot = primary_calendar.canonical()
        secondary_calendar_snapshot = secondary_calendar.canonical()
    plan = {
        "mode": mode, "business_end": end.isoformat(), "symbol_count": symbol_limit,
        "shard_size": SHARD_SIZE, "shard_count": shard_count,
        "current_snapshot": current_snapshot,
        "primary_calendar": primary_calendar_snapshot,
        "secondary_calendar": secondary_calendar_snapshot,
        "calendar_gates": [gate.canonical() for gate in calendar_gates],
        "calendar_source": calendar_source,
        "csi_discovered_notice_ids": [] if mode == "sample" else sorted(discovered),
        "csi_event_index_source": None if mode == "sample" else event_index_source,
        "matrix": {"include": [{"shard_index": index, "shard_count": shard_count} for index in range(shard_count)]},
    }
    plan["checkpoint_scope_sha256"] = sha256(plan)
    return plan


def _write_gzip(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(payload)


def write_outputs(
    output_dir: Path,
    manifest: dict[str, Any],
    bars: list[HistoricalBar],
    facts: list[TradeabilityFact],
    adjustments: list[AdjustmentEvent],
    references: list[SecurityReference],
    close_checks: list[tuple[str, date, Decimal, Decimal]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_gzip(output_dir / "historical-bars.json.gz", [row.canonical() for row in sorted(bars, key=lambda value: value.key)])
    _write_gzip(output_dir / "tradeability.json.gz", [row.canonical() for row in sorted(facts, key=lambda value: (value.symbol, value.business_date))])
    _write_gzip(output_dir / "adjustment-events.json.gz", [row.canonical() for row in sorted(adjustments, key=lambda value: (value.symbol, value.effective_date))])
    _write_gzip(output_dir / "verification-checks.json.gz", [
        {
            "symbol": symbol,
            "business_date": business_date.isoformat(),
            "primary_close": format(primary, "f"),
            "verification_close": format(verification, "f"),
        }
        for symbol, business_date, primary, verification in sorted(close_checks)
    ])
    (output_dir / "security-references.json").write_text(json.dumps([row.canonical() for row in sorted(references, key=lambda value: value.symbol)], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="M2.3 historical market-data acceptance")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--mode", choices=("sample", "smoke", "preflight", "full"), default="sample")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--plan-output", type=Path)
    parser.add_argument("--plan-input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("historical-market-acceptance"))
    parser.add_argument("--symbol-attempts", type=int, default=1)
    parser.add_argument(
        "--tidb-checkpoint-dataset-id",
        default=os.environ.get("TIDB_CHECKPOINT_DATASET_ID", ""),
        help="Stable TiDB dataset id; blank disables resumable per-symbol checkpoints",
    )
    args = parser.parse_args()
    if args.plan_output:
        plan = build_plan(args.end_date, args.mode)
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    current_universe = None
    csi_discovered_notice_ids = None
    primary_calendar = None
    secondary_calendar = None
    if args.plan_input:
        plan = json.loads(args.plan_input.read_text(encoding="utf-8"))
        if plan.get("mode") != args.mode:
            raise ValueError(f"plan mode {plan.get('mode')} does not match requested mode {args.mode}")
        if plan.get("business_end") != args.end_date.isoformat():
            raise ValueError(f"plan end date {plan.get('business_end')} does not match requested end date {args.end_date}")
        current_snapshot = plan.get("current_snapshot")
        if current_snapshot:
            current_universe = current_universe_from_canonical(current_snapshot)
        discovered = plan.get("csi_discovered_notice_ids")
        if discovered is not None:
            csi_discovered_notice_ids = {int(value) for value in discovered}
        primary_calendar_snapshot = plan.get("primary_calendar")
        secondary_calendar_snapshot = plan.get("secondary_calendar")
        if primary_calendar_snapshot:
            primary_calendar = trading_calendar_from_canonical(primary_calendar_snapshot)
        if secondary_calendar_snapshot:
            secondary_calendar = trading_calendar_from_canonical(secondary_calendar_snapshot)
    checkpoint_dataset_id = args.tidb_checkpoint_dataset_id.strip()
    resume_evidence = None
    checkpoint_writer: Callable[..., None] | None = None
    if checkpoint_dataset_id:
        from scripts.market_data.tidb_checkpoint_store import (
            HistoricalEvidence,
            TiDBConfig,
            connect,
            ensure_schema,
            load_resumable_evidence,
            publish_symbol_checkpoint,
        )

        tidb_config = TiDBConfig.from_env()
        resume_connection = connect(tidb_config)
        try:
            ensure_schema(resume_connection)
            resume_evidence = load_resumable_evidence(resume_connection, checkpoint_dataset_id)
        finally:
            resume_connection.close()
        _progress(
            "tidb_checkpoint_scope_ready", dataset_id=checkpoint_dataset_id,
            resumable_symbols=len(resume_evidence.manifest.get("resumed_symbols", [])),
        )

        def write_symbol_checkpoint(
            symbol: str,
            symbol_bars: list[HistoricalBar],
            symbol_facts: list[TradeabilityFact],
            symbol_adjustments: list[AdjustmentEvent],
            symbol_reference: SecurityReference | None,
            symbol_checks: list[tuple[str, date, Decimal, Decimal]],
            primary_source_name: str | None,
            error_message: str | None,
        ) -> None:
            checkpoint_manifest = {
                "manifest_version": "m2-historical-symbol-checkpoint-v1",
                "authoritative": False,
                "simulation_orders_allowed": False,
                "accepted": False,
                "mode": args.mode,
                "history_start": (HISTORY_START if args.mode in {"smoke", "preflight", "full"} else max(HISTORY_START, args.end_date - timedelta(days=150))).isoformat(),
                "business_end": args.end_date.isoformat(),
                "shard_index": args.shard_index,
                "shard_count": args.shard_count,
                "symbol_count": 1,
                "expected_key_count": len(symbol_facts),
                "primary_failures": {} if error_message is None else {symbol: error_message},
                "primary_sources_by_symbol": {} if primary_source_name is None else {symbol: primary_source_name},
            }
            checkpoint_evidence = HistoricalEvidence(
                manifest=checkpoint_manifest,
                bars=[row.canonical() for row in symbol_bars],
                tradeability=[row.canonical() for row in symbol_facts],
                adjustments=[row.canonical() for row in symbol_adjustments],
                references=[] if symbol_reference is None else [symbol_reference.canonical()],
                verification_checks=[{
                    "symbol": code, "business_date": business_date.isoformat(),
                    "primary_close": format(primary, "f"), "verification_close": format(verification, "f"),
                } for code, business_date, primary, verification in symbol_checks],
            )
            last_error: Exception | None = None
            for attempt in range(1, 4):
                checkpoint_connection = None
                try:
                    checkpoint_connection = connect(tidb_config)
                    publish_symbol_checkpoint(
                        checkpoint_connection, checkpoint_evidence, dataset_id=checkpoint_dataset_id,
                    )
                    return
                except Exception as error:
                    last_error = error
                    if checkpoint_connection is not None:
                        checkpoint_connection.rollback()
                    _progress(
                        "tidb_symbol_checkpoint_retry", symbol=symbol, attempt=attempt,
                        remaining_attempts=3 - attempt, error=f"{type(error).__name__}: {error}"[:300],
                    )
                    if attempt < 3:
                        time.sleep(min(2 ** (attempt - 1), 4))
                finally:
                    if checkpoint_connection is not None:
                        checkpoint_connection.close()
            assert last_error is not None
            raise last_error

        checkpoint_writer = write_symbol_checkpoint

    result = run(
        args.end_date, mode=args.mode, workers=args.workers,
        symbol_attempts=args.symbol_attempts,
        shard_index=args.shard_index, shard_count=args.shard_count,
        current_universe=current_universe,
        csi_discovered_notice_ids=csi_discovered_notice_ids,
        primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar,
        resume_evidence=resume_evidence,
        checkpoint_callback=checkpoint_writer,
    )
    manifest = result[0]
    if checkpoint_dataset_id:
        manifest["checkpoint_dataset_id"] = checkpoint_dataset_id
    write_outputs(args.output_dir, *result)
    print(json.dumps({key: manifest[key] for key in ("accepted", "mode", "business_end", "symbol_count", "expected_key_count", "bar_count", "tradeability_count", "adjustment_event_count", "bars_sha256", "tradeability_sha256", "adjustments_sha256", "gates")}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["accepted"] else 2


if __name__ == "__main__":
    sys.exit(main())

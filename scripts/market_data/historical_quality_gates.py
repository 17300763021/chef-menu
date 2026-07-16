"""Fail-closed M2.3 historical-market quality gates."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Iterable

from scripts.market_data.historical_contracts import AdjustmentEvent, HistoricalBar
from scripts.market_data.quality_gates import GateResult
from scripts.market_data.tradeability_contracts import TradeabilityFact


def evaluate_historical(
    *,
    expected_keys: set[tuple[str, date]],
    calendar_dates: set[date],
    bars: Iterable[HistoricalBar],
    facts: Iterable[TradeabilityFact],
    adjustments: Iterable[AdjustmentEvent],
    close_checks: Iterable[tuple[str, date, Decimal, Decimal]],
    verification_expected: int,
) -> list[GateResult]:
    bar_rows = list(bars)
    fact_rows = list(facts)
    close_rows = list(close_checks)
    adjustment_rows = list(adjustments)
    results: list[GateResult] = []

    duplicate_bars = [key for key, count in Counter(row.key for row in bar_rows).items() if count > 1]
    duplicate_facts = [key for key, count in Counter((row.symbol, row.business_date) for row in fact_rows).items() if count > 1]
    results.append(GateResult("historical_duplicate_bars", not duplicate_bars, len(duplicate_bars), "= 0"))
    results.append(GateResult("tradeability_duplicate_facts", not duplicate_facts, len(duplicate_facts), "= 0"))

    invalid_dates = [f"{row.symbol}:{row.business_date}" for row in bar_rows if row.business_date not in calendar_dates]
    invalid_ohlc = [
        f"{row.symbol}:{row.business_date}" for row in bar_rows
        if row.low > min(row.open, row.close) or row.high < max(row.open, row.close) or row.low > row.high
    ]
    invalid_units = [f"{row.symbol}:{row.business_date}" for row in bar_rows if row.volume_shares < 0 or row.amount_cny < 0]
    results.append(GateResult("historical_calendar_alignment", not invalid_dates, len(invalid_dates), "= 0", details=tuple(invalid_dates[:20])))
    results.append(GateResult("historical_ohlc_invariants", not invalid_ohlc, len(invalid_ohlc), "= 0", details=tuple(invalid_ohlc[:20])))
    results.append(GateResult("historical_nonnegative_units", not invalid_units, len(invalid_units), "= 0", details=tuple(invalid_units[:20])))

    fact_map = {(row.symbol, row.business_date): row for row in fact_rows}
    active_expected = {key for key in expected_keys if not fact_map.get(key) or not fact_map[key].is_suspended}
    observed = {row.key for row in bar_rows}
    coverage_bps = len(observed & active_expected) * 10000 // max(1, len(active_expected))
    missing = sorted(active_expected - observed)
    results.append(GateResult("historical_active_coverage", coverage_bps >= 9800, f"{coverage_bps / 100:.2f}%", ">= 98.00%", details=tuple(f"{s}:{d}" for s, d in missing[:20])))
    fact_coverage_bps = len(set(fact_map) & expected_keys) * 10000 // max(1, len(expected_keys))
    results.append(GateResult("tradeability_fact_coverage", fact_coverage_bps == 10000, f"{fact_coverage_bps / 100:.2f}%", "= 100.00%"))

    close_mismatches = []
    for symbol, business_date, primary, secondary in close_rows:
        tolerance = max(Decimal("0.01"), abs(primary) * Decimal("0.0005"))
        if abs(primary - secondary) > tolerance:
            close_mismatches.append(f"{symbol}:{business_date}:{primary}:{secondary}")
    close_bps = (len(close_rows) - len(close_mismatches)) * 10000 // max(1, len(close_rows))
    verification_coverage_bps = len(close_rows) * 10000 // max(1, verification_expected)
    results.append(GateResult(
        "historical_cross_source_coverage",
        verification_expected > 0 and verification_coverage_bps >= 9500,
        f"{len(close_rows)}/{verification_expected} ({verification_coverage_bps / 100:.2f}%)",
        ">= 95.00%",
    ))
    results.append(GateResult("historical_cross_source_close", bool(close_rows) and close_bps >= 9950, f"{close_bps / 100:.2f}%", ">= 99.50%", details=tuple(close_mismatches[:20])))

    invalid_adjusted = [
        f"{row.symbol}:{row.business_date}"
        for row in bar_rows
        if min(
            row.qfq_open, row.qfq_high, row.qfq_low, row.qfq_close,
            row.hfq_open, row.hfq_high, row.hfq_low, row.hfq_close,
        ) <= 0
    ]
    results.append(GateResult(
        "adjusted_price_completeness", not invalid_adjusted and len(bar_rows) == len(observed),
        len(invalid_adjusted), "= 0 missing or nonpositive adjusted rows",
        details=tuple(invalid_adjusted[:20]),
    ))

    bars_by_symbol: dict[str, list[HistoricalBar]] = {}
    for row in bar_rows:
        bars_by_symbol.setdefault(row.symbol, []).append(row)
    for rows in bars_by_symbol.values():
        rows.sort(key=lambda value: value.business_date)
    eligible_events = 0
    aligned_events = 0
    unaligned_events: list[str] = []
    factor_change_events: list[AdjustmentEvent] = []
    previous_factors: dict[str, tuple[Decimal, Decimal]] = {}
    for event in sorted(adjustment_rows, key=lambda value: (value.symbol, value.effective_date)):
        factors = (event.qfq_factor, event.hfq_factor)
        previous = previous_factors.get(event.symbol)
        if previous is None or factors != previous:
            factor_change_events.append(event)
        previous_factors[event.symbol] = factors
    for event in factor_change_events:
        rows = bars_by_symbol.get(event.symbol, [])
        if len(rows) < 2 or not (rows[0].business_date < event.effective_date <= rows[-1].business_date):
            continue
        position = next((index for index, row in enumerate(rows) if row.business_date >= event.effective_date), None)
        if position is None or position == 0:
            continue
        eligible_events += 1
        previous, current = rows[position - 1], rows[position]
        raw_return = current.close / previous.close - Decimal("1")
        qfq_return = current.qfq_close / previous.qfq_close - Decimal("1")
        hfq_return = current.hfq_close / previous.hfq_close - Decimal("1")
        if max(abs(qfq_return - raw_return), abs(hfq_return - raw_return)) > Decimal("0.00001"):
            aligned_events += 1
        else:
            unaligned_events.append(f"{event.symbol}:{event.effective_date}")
    event_alignment_bps = aligned_events * 10000 // max(1, eligible_events)
    results.append(GateResult(
        "corporate_action_adjustment_spot_check",
        eligible_events > 0 and event_alignment_bps >= 9500,
        f"{aligned_events}/{eligible_events} ({event_alignment_bps / 100:.2f}%)",
        ">= 95.00% factor-change action dates show adjusted/raw discontinuity correction",
        details=tuple(unaligned_events[:20]),
    ))

    common_blocks = {"missing_primary_bar", "missing_secondary_status", "suspended", "unknown_st_status"}
    unsafe_buy = [
        f"{row.symbol}:{row.business_date}" for row in fact_rows
        if row.can_buy and any(reason in common_blocks | {"one_price_limit_up"} for reason in row.block_reasons)
    ]
    unsafe_sell = [
        f"{row.symbol}:{row.business_date}" for row in fact_rows
        if row.can_sell and any(reason in common_blocks | {"one_price_limit_down"} for reason in row.block_reasons)
    ]
    results.append(GateResult("tradeability_fail_closed", not unsafe_buy and not unsafe_sell, len(unsafe_buy) + len(unsafe_sell), "= 0", details=tuple([*unsafe_buy[:10], *unsafe_sell[:10]])))
    return results

"""Point-in-time adjustment continuity for one M2 daily increment.

The historical M2.3 baseline is immutable.  A daily increment therefore never
rewrites old prices.  It carries the previous accepted factor forward on normal
sessions and requires an independently sourced factor event before accepting an
ex-rights/ex-dividend discontinuity.  Future consumers can use the stored event
lineage to rebase a complete QFQ series without changing the frozen evidence.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

from scripts.market_data.contracts import PRICE_QUANTUM, DailyBar, normalize_symbol
from scripts.market_data.historical_contracts import AdjustmentEvent, FACTOR_QUANTUM, HistoricalBar
from scripts.market_data.quality_gates import GateResult


PRICE_BREAK_TOLERANCE = Decimal("0.0005")
FACTOR_RATIO_TOLERANCE = Decimal("0.002")
A_SHARE_REFERENCE_QUANTUM = Decimal("0.01")
RQALPHA_DEFERRED_CASH_ACTION_SOURCE = "rqalpha_deferred_cash_action"


def _limited(values: Iterable[str], maximum: int = 20) -> tuple[str, ...]:
    return tuple(sorted(values)[:maximum])


def _within_rate(first: Decimal, second: Decimal, tolerance: Decimal) -> bool:
    if first <= 0 or second <= 0:
        return False
    return abs(first - second) / abs(first) <= tolerance


@dataclass(frozen=True, slots=True)
class PreviousAdjustedState:
    """Accepted predecessor values required to extend an adjusted series."""

    symbol: str
    business_date: date
    raw_close: Decimal
    qfq_factor: Decimal
    hfq_factor: Decimal
    source_dataset_id: str

    def __post_init__(self) -> None:
        if normalize_symbol(self.symbol) != self.symbol:
            raise ValueError("previous-state symbol must be a normalized six-digit code")
        if self.raw_close <= 0 or self.qfq_factor <= 0 or self.hfq_factor <= 0:
            raise ValueError("previous adjusted state requires positive close and factors")
        if not self.source_dataset_id.strip():
            raise ValueError("previous adjusted state requires a source dataset id")

    def canonical(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "business_date": self.business_date.isoformat(),
            "raw_close": format(self.raw_close, "f"),
            "qfq_factor": format(self.qfq_factor, "f"),
            "hfq_factor": format(self.hfq_factor, "f"),
            "source_dataset_id": self.source_dataset_id,
        }


def has_price_break(previous_close: Decimal, reported_previous_close: Decimal) -> bool:
    """Return true when the exchange reference price is not the prior raw close."""
    return not _within_rate(previous_close, reported_previous_close, PRICE_BREAK_TOLERANCE)


def _event_for_target(
    symbol: str,
    target_session: date,
    events: Iterable[AdjustmentEvent],
) -> AdjustmentEvent | None:
    matches = [
        event
        for event in events
        if event.symbol == symbol and event.effective_date == target_session
    ]
    if len(matches) > 1:
        raise ValueError(f"duplicate target-session adjustment events for {symbol}")
    return matches[0] if matches else None


def build_daily_adjusted_bars(
    *,
    target_session: date,
    previous_session: date,
    membership: Mapping[str, str],
    primary_bars: Iterable[DailyBar],
    previous_states: Mapping[str, PreviousAdjustedState],
    reported_previous_closes: Mapping[str, Decimal],
    adjustment_events: Iterable[AdjustmentEvent] = (),
    factor_reference_closes: Mapping[str, Decimal] | None = None,
) -> list[HistoricalBar]:
    """Extend immutable QFQ/HFQ factors for all observed target-session bars.

    A normal session carries both factors forward.  A reference-price break is
    accepted only when a separately attributed event exists on the target
    session.  Factor-ratio reconciliation remains mandatory unless an exact
    structured cash-dividend reference is supplied: absolute factors from two
    vendors do not share a guaranteed normalization base, so that comparison is
    diagnostic while RQAlpha remains responsible for applying the action.
    """
    events = list(adjustment_events)
    factor_references = factor_reference_closes or {}
    rows: list[HistoricalBar] = []
    for raw in sorted(primary_bars, key=lambda value: (value.symbol, value.business_date, value.source)):
        if raw.business_date != target_session or raw.symbol not in membership:
            raise ValueError(f"daily adjustment input is outside scope: {raw.symbol}:{raw.business_date}")
        state = previous_states.get(raw.symbol)
        if state is None or state.business_date != previous_session:
            raise ValueError(f"missing exact predecessor adjustment state for {raw.symbol}")
        reported = reported_previous_closes.get(raw.symbol)
        if reported is None or reported <= 0:
            raise ValueError(f"missing positive reported previous close for {raw.symbol}")
        event = _event_for_target(raw.symbol, target_session, events)
        price_break = has_price_break(state.raw_close, reported)
        if price_break and event is None:
            raise ValueError(f"unconfirmed previous-close discontinuity for {raw.symbol}")
        if event is None:
            qfq_factor = state.qfq_factor
            hfq_factor = state.hfq_factor
            factor_source = "accepted_predecessor_factor"
        else:
            if event.qfq_factor <= 0 or event.hfq_factor <= 0:
                raise ValueError(f"nonpositive target adjustment factor for {raw.symbol}")
            factor_reference = factor_references.get(raw.symbol, reported)
            if factor_reference <= 0:
                raise ValueError(f"nonpositive factor reference close for {raw.symbol}")
            if factor_reference.quantize(A_SHARE_REFERENCE_QUANTUM, rounding=ROUND_HALF_UP) != reported:
                raise ValueError(
                    f"factor reference close does not round to reported previous close for {raw.symbol}"
                )
            expected_hfq_ratio = (state.raw_close / factor_reference).quantize(
                FACTOR_QUANTUM, rounding=ROUND_HALF_UP,
            )
            observed_hfq_ratio = (event.hfq_factor / state.hfq_factor).quantize(
                FACTOR_QUANTUM, rounding=ROUND_HALF_UP,
            )
            exact_cash_reference = raw.symbol in factor_references
            factor_ratio_matches = _within_rate(
                expected_hfq_ratio, observed_hfq_ratio, FACTOR_RATIO_TOLERANCE,
            )
            if not exact_cash_reference and not factor_ratio_matches:
                raise ValueError(
                    f"adjustment factor does not reconcile for {raw.symbol}: "
                    f"expected ratio {expected_hfq_ratio}, observed {observed_hfq_ratio}"
                )
            if exact_cash_reference and not factor_ratio_matches:
                qfq_factor = state.qfq_factor
                hfq_factor = state.hfq_factor
                factor_source = RQALPHA_DEFERRED_CASH_ACTION_SOURCE
            else:
                qfq_factor = event.qfq_factor
                hfq_factor = event.hfq_factor
                factor_source = event.source
        rows.append(HistoricalBar.build(
            symbol=raw.symbol,
            business_date=raw.business_date,
            index_code=membership[raw.symbol],
            open_price=raw.open,
            high=raw.high,
            low=raw.low,
            close=raw.close,
            previous_close=reported,
            volume_shares=raw.volume_shares,
            amount_cny=raw.amount_cny,
            turnover_percent=raw.turnover_percent,
            qfq_factor=qfq_factor,
            hfq_factor=hfq_factor,
            primary_source=raw.source,
            factor_source=factor_source,
        ))
    return rows


def evaluate_daily_adjustments(
    *,
    target_session: date,
    previous_session: date,
    membership: Mapping[str, str],
    primary_bars: Iterable[DailyBar],
    adjusted_bars: Iterable[HistoricalBar],
    previous_states: Mapping[str, PreviousAdjustedState],
    reported_previous_closes: Mapping[str, Decimal],
    adjustment_events: Iterable[AdjustmentEvent],
    factor_reference_closes: Mapping[str, Decimal] | None = None,
) -> list[GateResult]:
    """Recheck adjustment completeness and arithmetic without trusting the builder."""
    raw_rows = list(primary_bars)
    adjusted_rows = list(adjusted_bars)
    events = list(adjustment_events)
    raw_map = {(row.symbol, row.business_date): row for row in raw_rows}
    adjusted_counts = Counter(row.key for row in adjusted_rows)
    adjusted_map = {row.key: row for row in adjusted_rows}
    duplicate_adjusted = [
        f"{symbol}:{business_date.isoformat()}"
        for (symbol, business_date), count in adjusted_counts.items()
        if count > 1
    ]
    missing_adjusted = [
        f"{symbol}:{business_date.isoformat()}"
        for symbol, business_date in sorted(set(raw_map) - set(adjusted_map))
    ]
    extra_adjusted = [
        f"{symbol}:{business_date.isoformat()}"
        for symbol, business_date in sorted(set(adjusted_map) - set(raw_map))
    ]
    scope_errors = [
        f"{row.symbol}:{row.business_date.isoformat()}"
        for row in adjusted_rows
        if row.business_date != target_session
        or membership.get(row.symbol) != row.index_code
    ]
    arithmetic_errors: list[str] = []
    lineage_errors: list[str] = []
    vendor_factor_mismatches: list[str] = []
    event_keys = Counter((event.symbol, event.effective_date) for event in events)
    duplicate_events = [
        f"{symbol}:{effective_date.isoformat()}"
        for (symbol, effective_date), count in event_keys.items()
        if count > 1
    ]
    event_scope_errors = [
        f"{event.symbol}:{event.effective_date.isoformat()}"
        for event in events
        if event.symbol not in membership or event.effective_date != target_session
    ]
    event_map = {(event.symbol, event.effective_date): event for event in events}
    factor_references = factor_reference_closes or {}

    for key in sorted(set(raw_map) & set(adjusted_map)):
        raw = raw_map[key]
        adjusted = adjusted_map[key]
        if (
            adjusted.open != raw.open or adjusted.high != raw.high
            or adjusted.low != raw.low or adjusted.close != raw.close
            or adjusted.volume_shares != raw.volume_shares
            or adjusted.amount_cny != raw.amount_cny
            or adjusted.primary_source != raw.source
            or min(adjusted.qfq_factor, adjusted.hfq_factor) <= 0
        ):
            arithmetic_errors.append(f"{raw.symbol}:raw_alignment")
            continue
        expected_prices = (
            (raw.open * adjusted.qfq_factor).quantize(PRICE_QUANTUM),
            (raw.high * adjusted.qfq_factor).quantize(PRICE_QUANTUM),
            (raw.low * adjusted.qfq_factor).quantize(PRICE_QUANTUM),
            (raw.close * adjusted.qfq_factor).quantize(PRICE_QUANTUM),
            (raw.open * adjusted.hfq_factor).quantize(PRICE_QUANTUM),
            (raw.high * adjusted.hfq_factor).quantize(PRICE_QUANTUM),
            (raw.low * adjusted.hfq_factor).quantize(PRICE_QUANTUM),
            (raw.close * adjusted.hfq_factor).quantize(PRICE_QUANTUM),
        )
        observed_prices = (
            adjusted.qfq_open, adjusted.qfq_high, adjusted.qfq_low, adjusted.qfq_close,
            adjusted.hfq_open, adjusted.hfq_high, adjusted.hfq_low, adjusted.hfq_close,
        )
        if observed_prices != expected_prices or min(observed_prices) <= 0:
            arithmetic_errors.append(f"{raw.symbol}:adjusted_arithmetic")

        state = previous_states.get(raw.symbol)
        reported = reported_previous_closes.get(raw.symbol)
        event = event_map.get((raw.symbol, target_session))
        if state is None or state.business_date != previous_session or reported is None or reported <= 0:
            lineage_errors.append(f"{raw.symbol}:missing_predecessor")
            continue
        price_break = has_price_break(state.raw_close, reported)
        if price_break != (event is not None):
            lineage_errors.append(f"{raw.symbol}:event_presence")
        elif event is None and (
            adjusted.qfq_factor != state.qfq_factor
            or adjusted.hfq_factor != state.hfq_factor
        ):
            lineage_errors.append(f"{raw.symbol}:unexpected_factor_change")
        elif event is not None:
            factor_reference = factor_references.get(raw.symbol, reported)
            if (
                factor_reference <= 0
                or factor_reference.quantize(A_SHARE_REFERENCE_QUANTUM, rounding=ROUND_HALF_UP) != reported
            ):
                lineage_errors.append(f"{raw.symbol}:invalid_factor_reference")
                continue
            expected_ratio = (state.raw_close / factor_reference).quantize(
                FACTOR_QUANTUM, rounding=ROUND_HALF_UP,
            )
            observed_ratio = (event.hfq_factor / state.hfq_factor).quantize(
                FACTOR_QUANTUM, rounding=ROUND_HALF_UP,
            )
            if not _within_rate(expected_ratio, observed_ratio, FACTOR_RATIO_TOLERANCE):
                if raw.symbol in factor_references:
                    vendor_factor_mismatches.append(
                        f"{raw.symbol}:expected={expected_ratio}:observed={observed_ratio}"
                    )
                    if (
                        adjusted.qfq_factor != state.qfq_factor
                        or adjusted.hfq_factor != state.hfq_factor
                        or adjusted.factor_source != RQALPHA_DEFERRED_CASH_ACTION_SOURCE
                    ):
                        lineage_errors.append(f"{raw.symbol}:deferred_factor_mismatch")
                else:
                    lineage_errors.append(f"{raw.symbol}:event_ratio_mismatch")
            elif (
                adjusted.qfq_factor != event.qfq_factor
                or adjusted.hfq_factor != event.hfq_factor
                or adjusted.factor_source != event.source
            ):
                lineage_errors.append(f"{raw.symbol}:event_factor_mismatch")

    return [
        GateResult(
            "daily_adjusted_duplicate_keys", not duplicate_adjusted,
            len(duplicate_adjusted), "= 0", details=_limited(duplicate_adjusted),
        ),
        GateResult(
            "daily_adjusted_primary_alignment", not missing_adjusted and not extra_adjusted,
            f"missing={len(missing_adjusted)} extra={len(extra_adjusted)}",
            "exactly one adjusted row per primary row",
            details=_limited([*missing_adjusted, *extra_adjusted]),
        ),
        GateResult(
            "daily_adjusted_scope", not scope_errors,
            len(scope_errors), "= 0", details=_limited(scope_errors),
        ),
        GateResult(
            "daily_adjusted_arithmetic", not arithmetic_errors,
            len(arithmetic_errors), "= 0", details=_limited(arithmetic_errors),
        ),
        GateResult(
            "daily_adjustment_event_duplicates", not duplicate_events,
            len(duplicate_events), "= 0", details=_limited(duplicate_events),
        ),
        GateResult(
            "daily_adjustment_event_scope", not event_scope_errors,
            len(event_scope_errors), "= 0 target-session universe events",
            details=_limited(event_scope_errors),
        ),
        GateResult(
            "daily_adjustment_lineage", not lineage_errors,
            len(lineage_errors), "= 0 unconfirmed price/factor discontinuities",
            details=_limited(lineage_errors),
        ),
        GateResult(
            "daily_vendor_absolute_factor_comparability", not vendor_factor_mismatches,
            len(vendor_factor_mismatches),
            "= 0 cross-vendor normalization mismatches; diagnostic when exact cash evidence exists",
            critical=False,
            details=_limited(vendor_factor_mismatches),
        ),
    ]

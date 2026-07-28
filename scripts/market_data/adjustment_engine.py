"""Deterministic factor filling and adjusted-price derivation."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from scripts.market_data.contracts import PRICE_QUANTUM, DailyBar, normalize_symbol
from scripts.market_data.historical_contracts import AdjustmentEvent


FACTOR_QUANTUM = Decimal("0.000001")


class AdjustmentTimeline:
    def __init__(self, events: list[AdjustmentEvent]) -> None:
        ordered = sorted(events, key=lambda value: value.effective_date)
        if len({event.effective_date for event in ordered}) != len(ordered):
            raise ValueError("duplicate adjustment effective date")
        self.events = ordered
        self.dates = [event.effective_date for event in ordered]

    def factors_on(self, business_date: date) -> tuple[Decimal, Decimal]:
        position = bisect_right(self.dates, business_date) - 1
        if position < 0:
            return Decimal("1"), Decimal("1")
        event = self.events[position]
        return event.qfq_factor, event.hfq_factor


def build_adjusted_series_from_factor_events(
    symbol: str,
    raw: dict[date, DailyBar],
    vendor_events: list[AdjustmentEvent],
    *,
    source: str,
) -> tuple[
    dict[date, tuple[Decimal, Decimal, Decimal, Decimal]],
    dict[date, tuple[Decimal, Decimal, Decimal, Decimal]],
    list[AdjustmentEvent],
]:
    """Build strictly positive multiplicative prices from a factor timeline.

    Sina's qfq factor is a divisor (latest value is 1), while its hfq factor is
    a multiplier (earliest value is 1).  Converting both to price multipliers
    avoids negative additive QFQ prices and retains exact, auditable effective
    dates instead of inferring corporate actions from rounded price series.
    """

    code = normalize_symbol(symbol)
    if not raw:
        raise ValueError(f"cannot adjust empty raw history for {code}")
    ordered = sorted(
        (event for event in vendor_events if event.symbol == code),
        key=lambda event: event.effective_date,
    )
    if not ordered or ordered[0].effective_date > min(raw):
        raise ValueError(f"factor timeline does not cover the first raw session for {code}")

    events: list[AdjustmentEvent] = []
    for event in ordered:
        if event.qfq_factor <= 0 or event.hfq_factor <= 0:
            raise ValueError(f"non-positive vendor factor for {code}:{event.effective_date}")
        qfq_multiplier = (Decimal("1") / event.qfq_factor).quantize(
            FACTOR_QUANTUM, rounding=ROUND_HALF_UP,
        )
        hfq_multiplier = event.hfq_factor.quantize(FACTOR_QUANTUM, rounding=ROUND_HALF_UP)
        if qfq_multiplier <= 0 or hfq_multiplier <= 0:
            raise ValueError(f"factor multiplier rounds to zero for {code}:{event.effective_date}")
        events.append(AdjustmentEvent(
            code, event.effective_date, qfq_multiplier, hfq_multiplier, source=source,
        ))

    timeline = AdjustmentTimeline(events)
    qfq_rows: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    hfq_rows: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for business_date, bar in sorted(raw.items()):
        qfq_factor, hfq_factor = timeline.factors_on(business_date)
        raw_prices = (bar.open, bar.high, bar.low, bar.close)
        qfq_prices = tuple(
            (value * qfq_factor).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
            for value in raw_prices
        )
        hfq_prices = tuple(
            (value * hfq_factor).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
            for value in raw_prices
        )
        if min(*qfq_prices, *hfq_prices) <= 0:
            raise ValueError(f"adjusted price rounds to zero for {code}:{business_date}")
        qfq_rows[business_date] = qfq_prices  # type: ignore[assignment]
        hfq_rows[business_date] = hfq_prices  # type: ignore[assignment]
    return qfq_rows, hfq_rows, events

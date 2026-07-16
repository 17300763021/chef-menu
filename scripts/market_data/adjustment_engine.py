"""Deterministic factor filling and adjusted-price derivation."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from decimal import Decimal

from scripts.market_data.historical_contracts import AdjustmentEvent


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

"""Canonical, source-labelled trading-calendar contracts for M2.2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable


CALENDAR_SCHEMA_VERSION = "m2-trading-calendar-v1"


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    source: str
    start_date: date
    end_date: date
    open_dates: tuple[date, ...]
    schema_version: str = CALENDAR_SCHEMA_VERSION

    @classmethod
    def build(cls, source: str, start_date: date, end_date: date, values: Iterable[date]) -> "TradingCalendar":
        dates = tuple(sorted(set(values)))
        if start_date > end_date:
            raise ValueError("calendar start_date is after end_date")
        if any(value < start_date or value > end_date for value in dates):
            raise ValueError("calendar contains a date outside the requested interval")
        return cls(source=source, start_date=start_date, end_date=end_date, open_dates=dates)

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "open_dates": [value.isoformat() for value in self.open_dates],
        }

    def next_session(self, value: date) -> date:
        for session in self.open_dates:
            if session > value:
                return session
        raise ValueError(f"no trading session after {value.isoformat()}")

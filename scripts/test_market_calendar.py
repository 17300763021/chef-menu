from __future__ import annotations

import unittest
from datetime import date

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.pit_quality_gates import evaluate_calendars
from scripts.market_data.quality_gates import accepted


class MarketCalendarTests(unittest.TestCase):
    def test_calendar_normalizes_and_finds_next_session(self) -> None:
        calendar = TradingCalendar.build("fixture", date(2026, 1, 1), date(2026, 1, 6), [date(2026, 1, 5), date(2026, 1, 2), date(2026, 1, 5)])
        self.assertEqual(calendar.open_dates, (date(2026, 1, 2), date(2026, 1, 5)))
        self.assertEqual(calendar.next_session(date(2026, 1, 2)), date(2026, 1, 5))

    def test_calendar_mismatch_fails_closed(self) -> None:
        first = TradingCalendar.build("a", date(2026, 1, 1), date(2026, 1, 3), [date(2026, 1, 2)])
        second = TradingCalendar.build("b", date(2026, 1, 1), date(2026, 1, 3), [date(2026, 1, 3)])
        self.assertFalse(accepted(evaluate_calendars(first, second)))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

from scripts.market_data.sources.baostock_calendar_source import BaostockCalendarSource


class _Response:
    def __init__(self, error_code: str = "0", error_msg: str = "") -> None:
        self.error_code = error_code
        self.error_msg = error_msg


class _TradeDateResult:
    error_code = "0"
    error_msg = ""
    fields = ["calendar_date", "is_trading_day"]

    def __init__(self) -> None:
        self.rows = [
            ["2026-07-01", "1"],
            ["2026-07-02", "0"],
            ["2026-07-03", "1"],
        ]
        self.index = -1

    def next(self) -> bool:
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self) -> list[str]:
        return self.rows[self.index]


class BaostockCalendarSourceTests(unittest.TestCase):
    def test_calendar_login_retries_transient_failures(self) -> None:
        fake = types.SimpleNamespace()
        fake.login_calls = 0
        fake.logout_calls = 0

        def login() -> _Response:
            fake.login_calls += 1
            if fake.login_calls < 3:
                return _Response("10002007", "网络接收错误")
            return _Response()

        def query_trade_dates(start_date: str, end_date: str) -> _TradeDateResult:
            self.assertEqual(start_date, "2026-07-01")
            self.assertEqual(end_date, "2026-07-03")
            return _TradeDateResult()

        def logout() -> None:
            fake.logout_calls += 1

        fake.login = login
        fake.query_trade_dates = query_trade_dates
        fake.logout = logout
        with patch.dict(sys.modules, {"baostock": fake}):
            calendar = BaostockCalendarSource(attempts=3, backoff_seconds=0).fetch(date(2026, 7, 1), date(2026, 7, 3))

        self.assertEqual(fake.login_calls, 3)
        self.assertEqual(fake.logout_calls, 1)
        self.assertEqual(calendar.open_dates, (date(2026, 7, 1), date(2026, 7, 3)))

    def test_calendar_login_failures_remain_fail_closed(self) -> None:
        fake = types.SimpleNamespace()
        fake.login_calls = 0

        def login() -> _Response:
            fake.login_calls += 1
            return _Response("10002007", "网络接收错误")

        fake.login = login
        fake.logout = lambda: None
        with patch.dict(sys.modules, {"baostock": fake}):
            with self.assertRaisesRegex(RuntimeError, "BaoStock calendar unavailable after 2 attempts"):
                BaostockCalendarSource(attempts=2, backoff_seconds=0).fetch(date(2026, 7, 1), date(2026, 7, 3))

        self.assertEqual(fake.login_calls, 2)


if __name__ == "__main__":
    unittest.main()

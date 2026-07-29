from __future__ import annotations

import sys
import socket
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


class _MidstreamCalendarFailure(_TradeDateResult):
    def next(self) -> bool:
        self.error_code = "10002007"
        self.error_msg = "network receive error"
        return False


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

    def test_calendar_socket_timeout_is_scoped_and_restored(self) -> None:
        fake = types.SimpleNamespace(
            login=lambda: _Response(),
            query_trade_dates=lambda **_kwargs: _TradeDateResult(),
            logout=lambda: None,
        )
        observed_timeouts: list[float | None] = []
        with (
            patch.dict(sys.modules, {"baostock": fake}),
            patch.object(socket, "getdefaulttimeout", return_value=7.0),
            patch.object(socket, "setdefaulttimeout", side_effect=observed_timeouts.append),
        ):
            calendar = BaostockCalendarSource(
                attempts=1, backoff_seconds=0, timeout_seconds=12.0,
            ).fetch(date(2026, 7, 1), date(2026, 7, 3))

        self.assertEqual(calendar.open_dates, (date(2026, 7, 1), date(2026, 7, 3)))
        self.assertEqual(observed_timeouts, [12.0, 7.0])

    def test_calendar_blacklist_fails_without_retry_and_restores_timeout(self) -> None:
        fake = types.SimpleNamespace(login_calls=0)

        def login() -> _Response:
            fake.login_calls += 1
            return _Response("10001011", "blacklisted")

        fake.login = login
        fake.logout = lambda: None
        observed_timeouts: list[float | None] = []
        with (
            patch.dict(sys.modules, {"baostock": fake}),
            patch.object(socket, "getdefaulttimeout", return_value=None),
            patch.object(socket, "setdefaulttimeout", side_effect=observed_timeouts.append),
        ):
            with self.assertRaisesRegex(RuntimeError, "calendar login blocked: 10001011"):
                BaostockCalendarSource(attempts=5, backoff_seconds=0).fetch(
                    date(2026, 7, 1), date(2026, 7, 3),
                )

        self.assertEqual(fake.login_calls, 1)
        self.assertEqual(observed_timeouts, [25.0, None])

    def test_calendar_socket_timeout_is_retried_with_limit(self) -> None:
        fake = types.SimpleNamespace(login_calls=0)

        def login() -> _Response:
            fake.login_calls += 1
            raise socket.timeout("fixture timeout")

        fake.login = login
        fake.logout = lambda: None
        with patch.dict(sys.modules, {"baostock": fake}):
            with self.assertRaisesRegex(RuntimeError, "calendar unavailable after 2 attempts"):
                BaostockCalendarSource(
                    attempts=2, backoff_seconds=0, timeout_seconds=1,
                ).fetch(date(2026, 7, 1), date(2026, 7, 3))

        self.assertEqual(fake.login_calls, 2)

    def test_calendar_midstream_network_error_is_retried(self) -> None:
        fake = types.SimpleNamespace(login_calls=0, logout_calls=0, query_calls=0)

        def login() -> _Response:
            fake.login_calls += 1
            return _Response()

        def query_trade_dates(**_kwargs):
            fake.query_calls += 1
            return _MidstreamCalendarFailure() if fake.query_calls == 1 else _TradeDateResult()

        fake.login = login
        fake.query_trade_dates = query_trade_dates
        fake.logout = lambda: setattr(fake, "logout_calls", fake.logout_calls + 1)
        with patch.dict(sys.modules, {"baostock": fake}):
            calendar = BaostockCalendarSource(
                attempts=2, backoff_seconds=0, timeout_seconds=1,
            ).fetch(date(2026, 7, 1), date(2026, 7, 3))

        self.assertEqual(calendar.open_dates, (date(2026, 7, 1), date(2026, 7, 3)))
        self.assertEqual((fake.login_calls, fake.query_calls, fake.logout_calls), (2, 2, 2))


if __name__ == "__main__":
    unittest.main()

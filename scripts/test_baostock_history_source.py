from __future__ import annotations

import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

from scripts.market_data.sources.baostock_history_source import BaostockHistorySource


class _Response:
    def __init__(self, error_code: str = "0", error_msg: str = "") -> None:
        self.error_code = error_code
        self.error_msg = error_msg


class _QueryResponse(_Response):
    def __init__(self, rows: list[list[str]] | None = None, error_code: str = "0", error_msg: str = "") -> None:
        super().__init__(error_code, error_msg)
        self.fields = [
            "date", "code", "open", "high", "low", "close", "preclose",
            "volume", "amount", "turn", "tradestatus", "isST",
        ]
        self._rows = rows or []
        self._position = -1

    def next(self) -> bool:
        self._position += 1
        return self._position < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._position]


class _MidstreamDisconnect(_QueryResponse):
    def next(self) -> bool:
        self.error_code = "10002007"
        self.error_msg = "network receive error"
        return False


class BaostockHistorySourceTests(unittest.TestCase):
    def test_blacklist_login_error_fails_closed_without_retry(self) -> None:
        fake = types.SimpleNamespace()
        fake.login_calls = 0

        def login() -> _Response:
            fake.login_calls += 1
            return _Response("10001011", "黑名单用户，请与管理员联系")

        fake.login = login
        fake.logout = lambda: None

        with patch.dict(sys.modules, {"baostock": fake}):
            with self.assertRaisesRegex(RuntimeError, "BaoStock login blocked: 10001011"):
                with BaostockHistorySource(attempts=3, timeout_seconds=1):
                    pass

        self.assertEqual(fake.login_calls, 1)

    def test_transient_login_error_still_retries(self) -> None:
        fake = types.SimpleNamespace()
        fake.login_calls = 0

        def login() -> _Response:
            fake.login_calls += 1
            if fake.login_calls == 1:
                return _Response("10002007", "网络接收错误")
            return _Response()

        fake.login = login
        fake.logout = lambda: None

        with patch.dict(sys.modules, {"baostock": fake}), patch("time.sleep", lambda _seconds: None):
            with BaostockHistorySource(attempts=2, timeout_seconds=1):
                pass

        self.assertEqual(fake.login_calls, 2)

    def test_transient_query_error_reconnects_and_retries_once(self) -> None:
        fake = types.SimpleNamespace(login_calls=0, logout_calls=0, query_calls=0)

        def login() -> _Response:
            fake.login_calls += 1
            return _Response()

        def logout() -> None:
            fake.logout_calls += 1

        def query(*_args, **_kwargs) -> _QueryResponse:
            fake.query_calls += 1
            if fake.query_calls == 1:
                return _QueryResponse(error_code="10002007", error_msg="network receive error")
            return _QueryResponse([[
                "2026-07-27", "sz.000001", "10.00", "10.10", "9.90", "10.00", "10.00",
                "10000", "100000.00", "0.10", "1", "0",
            ]])

        fake.login = login
        fake.logout = logout
        fake.query_history_k_data_plus = query
        with patch.dict(sys.modules, {"baostock": fake}), patch("time.sleep", lambda _seconds: None):
            with BaostockHistorySource(attempts=2, timeout_seconds=1) as source:
                rows = source.fetch_status("000001", date(2026, 7, 27), date(2026, 7, 27))

        self.assertEqual(rows[date(2026, 7, 27)]["tradestatus"], "1")
        self.assertEqual((fake.query_calls, fake.login_calls, fake.logout_calls), (2, 2, 2))

    def test_midstream_network_error_also_reconnects(self) -> None:
        fake = types.SimpleNamespace(login_calls=0, logout_calls=0, query_calls=0)
        fake.login = lambda: (setattr(fake, "login_calls", fake.login_calls + 1) or _Response())
        fake.logout = lambda: setattr(fake, "logout_calls", fake.logout_calls + 1)

        def query(*_args, **_kwargs) -> _QueryResponse:
            fake.query_calls += 1
            if fake.query_calls == 1:
                return _MidstreamDisconnect()
            return _QueryResponse([[
                "2026-07-27", "sz.000001", "10.00", "10.10", "9.90", "10.00", "10.00",
                "10000", "100000.00", "0.10", "1", "0",
            ]])

        fake.query_history_k_data_plus = query
        with patch.dict(sys.modules, {"baostock": fake}), patch("time.sleep", lambda _seconds: None):
            with BaostockHistorySource(attempts=2, timeout_seconds=1) as source:
                rows = source.fetch_status("000001", date(2026, 7, 27), date(2026, 7, 27))

        self.assertIn(date(2026, 7, 27), rows)
        self.assertEqual((fake.query_calls, fake.login_calls), (2, 2))

    def test_transient_query_error_stops_at_retry_limit(self) -> None:
        fake = types.SimpleNamespace(login_calls=0, logout_calls=0, query_calls=0)
        fake.login = lambda: (setattr(fake, "login_calls", fake.login_calls + 1) or _Response())
        fake.logout = lambda: setattr(fake, "logout_calls", fake.logout_calls + 1)

        def query(*_args, **_kwargs) -> _QueryResponse:
            fake.query_calls += 1
            return _QueryResponse(error_code="10002007", error_msg="network receive error")

        fake.query_history_k_data_plus = query
        with patch.dict(sys.modules, {"baostock": fake}), patch("time.sleep", lambda _seconds: None):
            with self.assertRaisesRegex(RuntimeError, "10002007"):
                with BaostockHistorySource(attempts=2, timeout_seconds=1) as source:
                    source.fetch_status("000001", date(2026, 7, 27), date(2026, 7, 27))

        self.assertEqual((fake.query_calls, fake.login_calls), (2, 2))

    def test_blacklist_query_error_fails_closed_without_reconnect(self) -> None:
        fake = types.SimpleNamespace(login_calls=0, logout_calls=0, query_calls=0)
        fake.login = lambda: (setattr(fake, "login_calls", fake.login_calls + 1) or _Response())
        fake.logout = lambda: setattr(fake, "logout_calls", fake.logout_calls + 1)

        def query(*_args, **_kwargs) -> _QueryResponse:
            fake.query_calls += 1
            return _QueryResponse(error_code="10001011", error_msg="blacklisted")

        fake.query_history_k_data_plus = query
        with patch.dict(sys.modules, {"baostock": fake}), patch("time.sleep", lambda _seconds: None):
            with self.assertRaisesRegex(RuntimeError, "10001011"):
                with BaostockHistorySource(attempts=3, timeout_seconds=1) as source:
                    source.fetch_status("000001", date(2026, 7, 27), date(2026, 7, 27))

        self.assertEqual((fake.query_calls, fake.login_calls), (1, 1))


if __name__ == "__main__":
    unittest.main()

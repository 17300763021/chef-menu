from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from scripts.market_data.sources.baostock_history_source import BaostockHistorySource


class _Response:
    def __init__(self, error_code: str = "0", error_msg: str = "") -> None:
        self.error_code = error_code
        self.error_msg = error_msg


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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from scripts.market_data.sources.eastmoney_market_state_source import (
    EastmoneySuspensionSource,
)
from scripts.market_data.sources.tencent_history_source import TencentIndexCalendarSource


class EastmoneyMarketStateSourceTests(unittest.TestCase):
    def test_dated_suspension_filters_intervals_without_guessing(self) -> None:
        fake = SimpleNamespace(
            stock_tfp_em=lambda **_kwargs: pd.DataFrame([
                [0, "000001", "one", date(2026, 7, 27), None],
                [1, "600000", "two", date(2026, 7, 28), None],
                [2, "300750", "three", date(2026, 7, 24), date(2026, 7, 24)],
            ]),
        )
        with patch.dict(sys.modules, {"akshare": fake}):
            suspended = EastmoneySuspensionSource(
                attempts=1, backoff_seconds=0, timeout_seconds=1,
            ).fetch(date(2026, 7, 27))
        self.assertEqual(suspended, frozenset({"000001"}))

    def test_tencent_calendar_filters_archive_overlap_by_requested_year(self) -> None:
        rows_by_year = {
            2025: [["2024-12-31"], ["2025-01-02"], ["2025-12-31"]],
            2026: [["2025-12-31"], ["2026-01-05"], ["2026-07-29"]],
        }

        def request(_source, _vendor_symbol, start, _end, _adjust):
            return rows_by_year[start.year]

        with patch(
            "scripts.market_data.sources.tencent_history_source.TencentHistorySource._request_block",
            new=request,
        ):
            calendar = TencentIndexCalendarSource(attempts=1).fetch(
                date(2025, 1, 1), date(2026, 7, 29),
            )
        self.assertEqual(calendar.open_dates, (
            date(2025, 1, 2), date(2025, 12, 31),
            date(2026, 1, 5), date(2026, 7, 29),
        ))


if __name__ == "__main__":
    unittest.main()

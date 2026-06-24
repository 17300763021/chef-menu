from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import sys
import unittest

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "stock_engine"))

import a_stock_trade_common_v7 as common


def history(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "date": pd.Timestamp(day),
            "open": price,
            "close": price,
            "high": price + 0.2,
            "low": price - 0.2,
            "volume": 1000,
        }
        for day, price in rows
    ])


class StockHistoryCacheTest(unittest.TestCase):
    def test_second_request_uses_local_cache(self) -> None:
        frame = history([
            ("2026-01-01", 10),
            ("2026-01-02", 10.2),
            ("2026-01-05", 10.4),
        ])
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "A_STOCK_HIST_CACHE_DIR": directory,
            "A_STOCK_HIST_CACHE_MAX_AGE_HOURS": "24",
        }, clear=False), patch.object(
            common, "_fetch_hist_remote", return_value=(frame, "测试日线")
        ) as fetch:
            first, first_source = common.get_hist("000001", days=3)
            second, second_source = common.get_hist("000001", days=3)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertIn("全量刷新", first_source)
        self.assertIn("本地缓存", second_source)

    def test_stale_cache_updates_overlap_without_duplicate_dates(self) -> None:
        initial = history([
            ("2026-01-01", 10),
            ("2026-01-02", 10.2),
            ("2026-01-05", 10.4),
        ])
        update = history([
            ("2026-01-05", 10.5),
            ("2026-01-06", 10.8),
        ])
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "A_STOCK_HIST_CACHE_DIR": directory,
            "A_STOCK_HIST_CACHE_MAX_AGE_HOURS": "0",
            "A_STOCK_HIST_CACHE_FULL_REFRESH_DAYS": "30",
        }, clear=False), patch.object(
            common, "_fetch_hist_remote", side_effect=[
                (initial, "测试日线"),
                (update, "测试日线"),
            ],
        ) as fetch:
            common.get_hist("000001", days=3)
            combined, source = common.get_hist("000001", days=3)

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(combined["date"].dt.strftime("%Y-%m-%d").tolist(), [
            "2026-01-02", "2026-01-05", "2026-01-06",
        ])
        self.assertEqual(float(combined.iloc[1]["close"]), 10.5)
        self.assertIn("增量更新", source)


if __name__ == "__main__":
    unittest.main()

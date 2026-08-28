from __future__ import annotations

import unittest

import pandas as pd

from scripts.market_data.index_bars import normalize_official, normalize_tencent
from scripts.market_data.index_quality_gates import evaluate_index_bars
from scripts.market_data.quality_gates import accepted


class IndexBarTests(unittest.TestCase):
    def test_units_and_cross_source_gates(self) -> None:
        official = pd.DataFrame([{
            "日期": "2026-07-31", "指数代码": "000300", "开盘": "10", "最高": "12", "最低": "9",
            "收盘": "11", "成交量": "123400", "成交金额": "1.5",
        }, {
            "日期": "2026-07-31", "指数代码": "000905", "开盘": "20", "最高": "22", "最低": "19",
            "收盘": "21", "成交量": "456700", "成交金额": "2.5",
        }])
        primary = normalize_official(official.iloc[[0]], "000300") + normalize_official(official.iloc[[1]], "000905")
        tx300 = pd.DataFrame([{"date": "2026-07-31", "open": "10", "high": "12", "low": "9", "close": "11", "amount": "1234"}])
        tx500 = pd.DataFrame([{"date": "2026-07-31", "open": "20", "high": "22", "low": "19", "close": "21", "amount": "4567"}])
        verification = normalize_tencent(tx300, "000300") + normalize_tencent(tx500, "000905")
        self.assertEqual(str(primary[0].amount_cny), "150000000.00")
        self.assertTrue(accepted(evaluate_index_bars(primary, verification)))

    def test_missing_verification_date_fails(self) -> None:
        frame = pd.DataFrame([{"日期": "2026-07-31", "指数代码": "000300", "开盘": 10, "最高": 12, "最低": 9, "收盘": 11, "成交量": 100, "成交金额": 1}])
        gates = evaluate_index_bars(normalize_official(frame, "000300"), [])
        self.assertFalse(next(g for g in gates if g.name == "index_cross_source_date_alignment").passed)


if __name__ == "__main__":
    unittest.main()

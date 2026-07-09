from pathlib import Path
import sys
import types
import unittest

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "stock_engine"))

import a_stock_night_scanner_v7 as scanner


def make_args() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        min_score=70,
        strong_min_score=76,
        trend_min_score=85,
        trend_min_day_pct=-5.5,
        trend_max_day_pct=4.5,
        trend_max_rsi=74.0,
        trend_min_pressure_room=0.5,
        trend_max_5d_pct=12.0,
        trend_min_amount=1.5e8,
        trend_min_turnover=0.3,
        trend_max_turnover=15.0,
        review_high_pe=300.0,
        review_high_turnover=12.0,
        review_high_amplitude=9.0,
        review_main_outflow_ratio=8.0,
        review_max_5d_pct=12.0,
        review_min_60d_pct=-25.0,
        review_max_60d_pct=80.0,
        review_min_pressure_room=1.5,
    )


def fake_score(score: int = 82) -> dict:
    return {
        "score": score,
        "last_close": 10.0,
        "action": "watch",
        "signal": "buy point",
        "ma20": 9.5,
        "vol_ratio": 1.2,
        "rsi14": 55.0,
        "atr14": 0.5,
        "support1": 9.0,
        "support2": 8.5,
        "pressure1": 11.0,
        "pressure2": 12.0,
        "stop": 8.8,
        "support_zone_low": 8.9,
        "support_zone_high": 9.2,
        "pressure_zone_low": 10.8,
        "pressure_zone_high": 11.2,
        "box_low": 8.5,
        "box_high": 11.5,
        "neckline": 10.5,
        "false_break_risk": False,
        "zone_note": "",
        "reasons": ["test reason"],
        "risks": [],
        "factor_scores": {
            "trend": 71,
            "momentum": 62,
            "volume": 58,
            "flow": 79,
            "quality": 66,
        },
    }


class NightScannerMultiFactorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_get_hist = scanner.get_hist
        self.original_multi_factor_score = scanner.multi_factor_score
        self.original_score_stock = scanner.score_stock

    def tearDown(self) -> None:
        scanner.get_hist = self.original_get_hist
        scanner.multi_factor_score = self.original_multi_factor_score
        scanner.score_stock = self.original_score_stock

    def test_analyze_stock_uses_multifactor_inputs_and_emits_factor_columns(self) -> None:
        calls = []
        scanner.get_hist = lambda code, days=360: (pd.DataFrame({"close": [1, 2, 3]}), "fixture")

        def fake_multi_factor(hist, code="", capital_flow=None, sector_data=None, market_state="震荡"):
            calls.append((code, capital_flow, sector_data, market_state))
            return fake_score()

        scanner.multi_factor_score = fake_multi_factor
        item = scanner.analyze_stock(
            pd.Series({"code": "600001", "name": "Alpha", "industry": "Tech"}),
            make_args(),
            capital_flow_cache={"600001": {"main_net_inflow_ratio": 8}},
            sector_data_cache={"600001": {"sector_return_60d": 3, "sector_rank": 4}},
            market_state="强牛市",
        )

        self.assertIsNotNone(item)
        self.assertEqual(calls[0][0], "600001")
        self.assertEqual(calls[0][1]["main_net_inflow_ratio"], 8)
        self.assertEqual(calls[0][2]["sector_rank"], 4)
        self.assertEqual(calls[0][3], "强牛市")
        self.assertEqual(item["因子趋势"], 71)
        self.assertEqual(item["因子动量"], 62)
        self.assertEqual(item["因子量价"], 58)
        self.assertEqual(item["因子资金"], 79)
        self.assertEqual(item["因子质量"], 66)
        self.assertEqual(item["行业排名"], 4)

    def test_analyze_stock_falls_back_to_score_stock_when_multifactor_fails(self) -> None:
        scanner.get_hist = lambda code, days=360: (pd.DataFrame({"close": [1, 2, 3]}), "fixture")
        scanner.multi_factor_score = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        scanner.score_stock = lambda hist: fake_score(score=77)

        item = scanner.analyze_stock(
            pd.Series({"code": "600002", "name": "Beta", "industry": "Bank"}),
            make_args(),
        )

        self.assertIsNotNone(item)
        self.assertEqual(item["因子趋势"], "")
        self.assertEqual(item["因子资金"], "")


if __name__ == "__main__":
    unittest.main()

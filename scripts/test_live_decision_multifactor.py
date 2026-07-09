from pathlib import Path
import sys
import unittest

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "stock_engine"))

import a_stock_live_decision_v8 as live
from a_stock_trade_common_v7 import decision_from_realtime


def fake_score() -> dict:
    return {
        "score": 80,
        "last_close": 10.0,
        "signal": "多因子测试",
        "support1": 9.8,
        "support2": 9.4,
        "pressure1": 10.15,
        "pressure2": 11.0,
        "stop": 9.3,
        "atr14": 0.2,
        "support_zone_low": 9.6,
        "support_zone_high": 9.9,
        "pressure_zone_low": 10.15,
        "pressure_zone_high": 11.0,
        "box_low": float("nan"),
        "box_high": float("nan"),
        "neckline": float("nan"),
        "false_break_risk": False,
        "zone_note": "",
        "reasons": ["factor reason"],
        "risks": [],
        "factor_scores": {"trend": 65, "momentum": 82, "volume": 55, "flow": 25, "quality": 60},
    }


def realtime_inputs():
    candidate = {
        "代码": "600001",
        "名称": "Alpha",
        "昨收": 10.0,
        "信号": "多因子测试",
        "支撑1": 9.8,
        "支撑2": 9.4,
        "压力1": 10.15,
        "压力2": 11.0,
        "建议止损": 9.3,
        "ATR14": 0.2,
        "factor_scores": {"trend": 65, "momentum": 82, "volume": 55, "flow": 25, "quality": 60},
    }
    rt = {"price": 10.1, "pct": 1.0, "open": 10.0, "high": 10.12, "low": 9.96, "volume_ratio": 1.1}
    minute = pd.DataFrame([
        {"close": 10.0, "avg_price": 9.98, "low": 9.96, "high": 10.02},
        {"close": 10.1, "avg_price": 10.0, "low": 10.0, "high": 10.12},
    ])
    market = {"市场环境": "强势", "市场建议": ""}
    sector = {"板块强弱": "强", "相对强弱": "强于板块", "板块持续性": "持续走强"}
    return candidate, rt, minute, sector, market


class LiveDecisionMultiFactorTest(unittest.TestCase):
    def test_make_candidate_uses_multi_factor_score_and_keeps_factor_scores(self) -> None:
        old_get_hist = live.get_hist
        old_multi = live.multi_factor_score
        try:
            live.get_hist = lambda code, days=360: (pd.DataFrame({"close": [1] * 40}), "fixture")
            live.multi_factor_score = lambda hist, code="": fake_score()

            candidate = live.make_candidate_from_code("600001", "Alpha")

            self.assertEqual(candidate["因子动量"], 82)
            self.assertEqual(candidate["因子资金"], 25)
            self.assertEqual(candidate["factor_scores"]["flow"], 25)
        finally:
            live.get_hist = old_get_hist
            live.multi_factor_score = old_multi

    def test_realtime_decision_uses_factor_scores_for_risk_and_upgrade(self) -> None:
        candidate, rt, minute, sector, market = realtime_inputs()

        result = decision_from_realtime(
            candidate,
            rt,
            minute,
            mode="buy",
            sector_context=sector,
            market_context=market,
        )

        self.assertIn("资金面偏弱，新买需谨慎", result["风险提示"])
        self.assertEqual(result["买入建议"], "可以买小仓")


if __name__ == "__main__":
    unittest.main()

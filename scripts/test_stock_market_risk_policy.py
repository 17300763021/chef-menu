from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "stock_engine"))

from a_stock_trade_common_v7 import market_risk_policy, strategy_risk_count
from paper_trade_engine import initial_position_rate


class StockMarketRiskPolicyTest(unittest.TestCase):
    def test_weak_market_allows_small_trial_position(self) -> None:
        policy = market_risk_policy("弱势")

        self.assertFalse(policy["check_ok"])
        self.assertFalse(policy["hard_block"])
        self.assertEqual(policy["position_rate"], 0.03)

    def test_paper_trade_uses_three_percent_for_weak_market_signal(self) -> None:
        rate = initial_position_rate({"final_action": "弱势市场仅可3%试错仓"})

        self.assertEqual(rate, 0.03)

    def test_missing_sector_data_does_not_consume_strategy_risk_slot(self) -> None:
        count = strategy_risk_count([
            "大盘环境弱，仅允许3%试错仓",
            "板块数据缺失，不能确认主线强度，新买降级",
        ])

        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

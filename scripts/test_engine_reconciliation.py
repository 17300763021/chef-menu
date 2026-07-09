"""Verify backtest and paper trading exit rules stay aligned."""

from datetime import date, timedelta
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_engine import backtest_exit_decision
from paper_trade_engine import MAX_HOLD_DAYS as PAPER_MAX_HOLD_DAYS
from paper_trade_engine import sell_decision


class EngineReconciliationTest(unittest.TestCase):
    def test_stop_loss_same_threshold(self) -> None:
        position = {
            "code": "000001",
            "shares": 5000,
            "cost_price": 10.0,
            "entry_stop_loss": 9.4,
            "sell_stage": "none",
            "trailing_stop_price": 0,
            "buy_date": date.today().isoformat(),
        }
        decision_stop_triggered = {
            "current_price": 9.3,
            "stop_loss": 9.4,
            "target_price_1": 12.0,
        }
        result = sell_decision(decision_stop_triggered, position)
        self.assertIn("原始止损", result.reason)
        self.assertEqual(result.shares, 5000)

        decision_no_trigger = {
            "current_price": 9.5,
            "stop_loss": 9.4,
            "target_price_1": 12.0,
        }
        result2 = sell_decision(decision_no_trigger, position)
        self.assertEqual(result2.reason, "")

    def test_time_stop_triggers_on_loss_position(self) -> None:
        old_buy_date = (date.today() - timedelta(days=PAPER_MAX_HOLD_DAYS + 1)).isoformat()
        position = {
            "code": "000001",
            "shares": 5000,
            "cost_price": 10.0,
            "entry_stop_loss": 9.4,
            "sell_stage": "none",
            "trailing_stop_price": 0,
            "buy_date": old_buy_date,
        }
        decision = {
            "current_price": 9.8,
            "stop_loss": 9.4,
            "target_price_1": 12.0,
        }
        result = sell_decision(decision, position)
        self.assertIn("时间止损", result.reason)
        self.assertGreater(result.shares, 0)

    def test_time_stop_does_not_trigger_on_profit_position(self) -> None:
        old_buy_date = (date.today() - timedelta(days=PAPER_MAX_HOLD_DAYS + 1)).isoformat()
        position = {
            "code": "000001",
            "shares": 5000,
            "cost_price": 10.0,
            "entry_stop_loss": 9.4,
            "sell_stage": "none",
            "trailing_stop_price": 0,
            "buy_date": old_buy_date,
        }
        decision = {
            "current_price": 11.5,
            "stop_loss": 9.4,
            "target_price_1": 12.0,
        }
        result = sell_decision(decision, position)
        self.assertNotIn("时间止损", result.reason)

    def test_entry_stop_loss_used_over_decision_stop(self) -> None:
        position = {
            "code": "000001",
            "shares": 5000,
            "cost_price": 10.0,
            "entry_stop_loss": 9.4,
            "sell_stage": "none",
            "trailing_stop_price": 0,
            "buy_date": date.today().isoformat(),
        }
        decision = {
            "current_price": 9.0,
            "stop_loss": 8.5,
            "target_price_1": 12.0,
        }
        result = sell_decision(decision, position)
        self.assertIn("原始止损", result.reason)

    def test_backtest_uses_intraday_low_for_same_stop_rule(self) -> None:
        result = backtest_exit_decision(
            entry_price=10.0,
            shares=5000,
            stop_price=9.4,
            target_price=12.0,
            close=9.6,
            low=9.3,
            high=9.8,
            holding_days=1,
            entry_date=date.today(),
            current_date=date.today(),
        )

        self.assertEqual(result.reason, "stop_loss")
        self.assertIn("原始止损", result.paper_reason)

    def test_backtest_uses_paper_time_stop_threshold(self) -> None:
        result = backtest_exit_decision(
            entry_price=10.0,
            shares=5000,
            stop_price=9.4,
            target_price=12.0,
            close=9.8,
            low=9.7,
            high=10.0,
            holding_days=PAPER_MAX_HOLD_DAYS,
            entry_date=date.today() - timedelta(days=PAPER_MAX_HOLD_DAYS),
            current_date=date.today(),
        )

        self.assertEqual(result.reason, "max_hold")
        self.assertIn("时间止损", result.paper_reason)


if __name__ == "__main__":
    unittest.main()

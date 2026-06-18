from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from strategy_replay import (
    compare_variants,
    indicator_diagnostics,
    portfolio_backtest,
    simulate_trade,
)


class StrategyReplayTest(unittest.TestCase):
    def test_indicator_diagnostics_flags_harmful_risk_gate(self) -> None:
        rows = [
            {"risks": ["大盘弱势"], "return_5d": 8.0, "max_return_10d": 12.0},
            {"risks": ["大盘弱势"], "return_5d": 4.0, "max_return_10d": 9.0},
            {"risks": [], "return_5d": -2.0, "max_return_10d": 1.0},
            {"risks": [], "return_5d": 0.0, "max_return_10d": 2.0},
        ]

        result = indicator_diagnostics(rows, "risks")

        weak_market = next(item for item in result if item["indicator"] == "大盘弱势")
        self.assertEqual(weak_market["sample_count"], 2)
        self.assertEqual(weak_market["avg_return_5d"], 6.0)
        self.assertEqual(weak_market["return_lift_5d"], 7.0)
        self.assertEqual(weak_market["missed_runner_count"], 2)

    def test_compare_variants_ranks_by_return_and_drawdown(self) -> None:
        trades = [
            {"variant": "strict", "signal_date": "2026-01-01", "pnl_rate": 2.0},
            {"variant": "strict", "signal_date": "2026-01-02", "pnl_rate": -1.0},
            {"variant": "balanced", "signal_date": "2026-01-01", "pnl_rate": 4.0},
            {"variant": "balanced", "signal_date": "2026-01-02", "pnl_rate": 1.0},
        ]

        result = compare_variants(trades)

        self.assertEqual(result[0]["variant"], "balanced")
        self.assertEqual(result[0]["trade_count"], 2)
        self.assertEqual(result[0]["win_rate"], 100.0)
        self.assertEqual(result[0]["batch_return_rate"], 0.4)

    def test_portfolio_does_not_rebuy_stock_while_holding(self) -> None:
        trades = [
            {
                "code": "000001", "entry_date": "2026-01-02", "exit_date": "2026-01-06",
                "entry_price": 10, "exit_price": 11, "score": 90, "market_regime": "sideways",
                "holding_days": 3, "price_path": {
                    "2026-01-02": 10, "2026-01-05": 10.5, "2026-01-06": 11,
                },
            },
            {
                "code": "000001", "entry_date": "2026-01-05", "exit_date": "2026-01-07",
                "entry_price": 10.5, "exit_price": 11.2, "score": 88, "market_regime": "sideways",
                "holding_days": 2, "price_path": {"2026-01-05": 10.5, "2026-01-07": 11.2},
            },
        ]

        result = portfolio_backtest(trades)

        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(result["rejected_signals"][0]["reject_reason"], "already_holding")
        self.assertEqual(result["executed_trades"][0]["shares"], 7900)

    def test_weak_market_uses_three_percent_position(self) -> None:
        trades = [{
            "code": "000001", "entry_date": "2026-01-02", "exit_date": "2026-01-05",
            "entry_price": 10, "exit_price": 10.5, "score": 90, "market_regime": "weak",
            "holding_days": 2, "price_path": {"2026-01-02": 10, "2026-01-05": 10.5},
        }]

        result = portfolio_backtest(trades)

        self.assertEqual(result["executed_trades"][0]["position_rate"], 0.03)
        self.assertEqual(result["executed_trades"][0]["shares"], 2900)

    def test_trade_exit_starts_after_entry_day_for_t_plus_one(self) -> None:
        frame = pd.DataFrame([
            {"date": pd.Timestamp("2026-01-01"), "open": 10, "high": 10.2, "low": 9.8, "close": 10},
            {"date": pd.Timestamp("2026-01-02"), "open": 10, "high": 12, "low": 9, "close": 11},
            {"date": pd.Timestamp("2026-01-05"), "open": 10.5, "high": 10.8, "low": 9.4, "close": 9.6},
        ])

        trade = simulate_trade(frame, 0, 9.5, "000001")

        self.assertIsNotNone(trade)
        self.assertEqual(trade["exit_date"], "2026-01-05")
        self.assertEqual(trade["exit_reason"], "stop")


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_engine import net_trade_result, summarize


class BacktestEngineMetricsTest(unittest.TestCase):
    def test_net_trade_result_applies_slippage_and_fees(self) -> None:
        result = net_trade_result(entry_price=10, exit_price=11, shares=1000)

        self.assertLess(result["entry_price"], 10.02)
        self.assertGreater(result["entry_price"], 10)
        self.assertLess(result["exit_price"], 11)
        self.assertGreater(result["fee_amount"], 0)
        self.assertGreater(result["slippage_amount"], 0)
        self.assertLess(result["pnl_amount"], 1000)

    def test_summarize_reports_professional_risk_metrics(self) -> None:
        trades = [
            {"pnl_amount": 10000, "pnl_rate": 10, "holding_days": 2, "exit_date": "2026-01-02", "entry_price": 10, "shares": 1000},
            {"pnl_amount": -20000, "pnl_rate": -20, "holding_days": 3, "exit_date": "2026-01-03", "entry_price": 10, "shares": 1000},
            {"pnl_amount": -5000, "pnl_rate": -5, "holding_days": 2, "exit_date": "2026-01-04", "entry_price": 10, "shares": 1000},
            {"pnl_amount": 15000, "pnl_rate": 15, "holding_days": 4, "exit_date": "2026-01-05", "entry_price": 10, "shares": 1000},
        ]

        metrics = summarize(trades)

        self.assertEqual(metrics["largest_single_loss"], -20000)
        self.assertEqual(metrics["consecutive_losses"], 2)
        self.assertEqual(metrics["turnover_rate"], 4.0)
        self.assertAlmostEqual(metrics["max_drawdown_rate"], 2.48, places=2)
        self.assertIn("sharpe_ratio", metrics)
        self.assertIn("calmar_ratio", metrics)


if __name__ == "__main__":
    unittest.main()

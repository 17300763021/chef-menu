from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy_replay import compare_variants, indicator_diagnostics


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


if __name__ == "__main__":
    unittest.main()

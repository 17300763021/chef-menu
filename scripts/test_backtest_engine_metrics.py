from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_engine import (
    benchmark_return_from_history,
    build_date_splits,
    equity_curve,
    parameter_sensitivity_cases,
    insert_results,
    reconcile_equity_curve,
    split_trade_metrics,
    net_trade_result,
    recent_picks,
    summarize,
    stock_history,
)


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

    def test_trade_list_reconciles_with_equity_curve(self) -> None:
        trades = [
            {"exit_date": "2026-01-03", "pnl_amount": 1000},
            {"exit_date": "2026-01-04", "pnl_amount": -250},
        ]
        curve = equity_curve(trades, benchmark_rate=0)

        reconciliation = reconcile_equity_curve(trades, curve)

        self.assertTrue(reconciliation["ok"])
        self.assertEqual(reconciliation["trade_count"], 2)
        self.assertAlmostEqual(reconciliation["expected_final_value"], 1000750)
        self.assertAlmostEqual(reconciliation["actual_final_value"], 1000750)

    def test_trade_list_reconciliation_catches_curve_mismatch(self) -> None:
        trades = [{"exit_date": "2026-01-03", "pnl_amount": 1000}]
        broken_curve = [{"curve_date": "2026-01-03", "equity_value": 1000500}]

        reconciliation = reconcile_equity_curve(trades, broken_curve)

        self.assertFalse(reconciliation["ok"])
        self.assertEqual(reconciliation["mismatch_count"], 1)

    def test_build_date_splits_covers_period_without_overlap(self) -> None:
        splits = build_date_splits("2026-01-01", "2026-04-10")

        self.assertEqual([item["name"] for item in splits], ["in_sample", "validation", "test", "out_of_sample"])
        self.assertEqual(splits[0]["start_date"], "2026-01-01")
        self.assertEqual(splits[-1]["end_date"], "2026-04-10")
        for left, right in zip(splits, splits[1:]):
            self.assertLess(left["end_date"], right["start_date"])

    def test_build_date_splits_handles_short_periods_without_invalid_ranges(self) -> None:
        splits = build_date_splits("2026-07-01", "2026-07-03")

        for split in splits:
            if split["start_date"] and split["end_date"]:
                self.assertLessEqual(split["start_date"], split["end_date"])

    def test_split_trade_metrics_reports_each_sample_period(self) -> None:
        splits = build_date_splits("2026-01-01", "2026-04-10")
        trades = [
            {"exit_date": "2026-01-10", "pnl_amount": 1000, "pnl_rate": 1, "holding_days": 2, "entry_price": 10, "shares": 1000},
            {"exit_date": "2026-03-15", "pnl_amount": -500, "pnl_rate": -0.5, "holding_days": 3, "entry_price": 10, "shares": 1000},
            {"exit_date": "2026-04-05", "pnl_amount": 1500, "pnl_rate": 1.5, "holding_days": 2, "entry_price": 10, "shares": 1000},
        ]

        metrics = split_trade_metrics(trades, splits)

        self.assertEqual(set(metrics), {"in_sample", "validation", "test", "out_of_sample"})
        self.assertEqual(sum(item["trade_count"] for item in metrics.values()), 3)

    def test_split_trade_metrics_handles_empty_short_period_splits(self) -> None:
        splits = build_date_splits("2026-07-01", "2026-07-03")

        metrics = split_trade_metrics([], splits)

        self.assertEqual(set(metrics), {"in_sample", "validation", "test", "out_of_sample"})
        self.assertEqual(metrics["out_of_sample"]["trade_count"], 0)

    def test_parameter_sensitivity_cases_are_deterministic_and_bounded(self) -> None:
        cases = parameter_sensitivity_cases()

        self.assertEqual(cases, parameter_sensitivity_cases())
        self.assertGreaterEqual(len(cases), 9)
        self.assertEqual(cases[0]["case_name"], "tp_0.08_sl_0.04_hold_5")
        self.assertTrue(all(0 < item["take_profit_rate"] < 1 for item in cases))
        self.assertTrue(all(0 < item["stop_rate"] < 1 for item in cases))

    def test_benchmark_return_from_history_uses_first_and_last_close(self) -> None:
        history = [
            {"date": "2026-01-01", "close": 100},
            {"date": "2026-01-02", "close": 110},
        ]

        self.assertEqual(benchmark_return_from_history(history), 10)

    def test_benchmark_return_from_pandas_history(self) -> None:
        import pandas as pd

        history = pd.DataFrame([
            {"date": "2026-01-01", "close": 100},
            {"date": "2026-01-02", "close": 90},
        ])

        self.assertEqual(benchmark_return_from_history(history), -10)

    def test_dry_run_payload_includes_audit_fields(self) -> None:
        picks = [{"scan_date": "2026-01-01"}]
        trades = [
            {
                "exit_date": "2026-01-02",
                "pnl_amount": 1000,
                "pnl_rate": 1,
                "holding_days": 1,
                "entry_price": 10,
                "shares": 1000,
            }
        ]
        curve = equity_curve(trades, benchmark_rate=2)

        result = insert_results(
            client=None,
            picks=picks,
            trades=trades,
            missed=[],
            curve=curve,
            benchmark_rate=2,
            dry_run=True,
            benchmark_details={"csi300": 1.5, "csi500": 2.5},
        )

        run = result["run"]
        self.assertTrue(run["equity_reconciled"])
        self.assertEqual(run["benchmark_csi300_return_rate"], 1.5)
        self.assertEqual(run["benchmark_csi500_return_rate"], 2.5)
        self.assertIn("in_sample", run["sample_split_summary"])
        self.assertGreaterEqual(len(run["parameter_sensitivity_summary"]), 9)

    def test_stock_history_falls_back_to_supabase_cache_when_akshare_fails(self) -> None:
        import pandas as pd

        class FailingAk:
            def stock_zh_a_hist(self, **kwargs):
                raise RuntimeError("akshare unavailable")

        class FakeClient:
            def request(self, method, path):
                self.last_request = (method, path)
                return [
                    {"trade_date": "2026-01-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000},
                    {"trade_date": "2026-01-02", "open": 10.5, "high": 12, "low": 10, "close": 11.5, "volume": 1200},
                ]

        client = FakeClient()

        with patch("backtest_engine.load_backtest_dependencies", return_value=(FailingAk(), None, pd)):
            with patch("backtest_engine.get_client", return_value=client):
                history = stock_history("000001", "2026-01-01", "2026-01-02")

        self.assertEqual(len(history), 2)
        self.assertEqual(float(history.iloc[-1]["close"]), 11.5)
        self.assertIn("stock_daily_history", client.last_request[1])

    def test_stock_history_can_use_supabase_cache_first(self) -> None:
        import pandas as pd

        cache_frame = pd.DataFrame([
            {"open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000},
            {"open": 10.5, "high": 12, "low": 10, "close": 11.5, "volume": 1200},
        ], index=pd.to_datetime(["2026-01-01", "2026-01-02"]))

        with patch.dict(os.environ, {"STOCK_BACKTEST_HISTORY_SOURCE": "cache"}):
            with patch("backtest_engine.cached_stock_history", return_value=cache_frame) as cache_mock:
                with patch("backtest_engine.load_backtest_dependencies") as deps_mock:
                    history = stock_history("000001", "2026-01-01", "2026-01-02")

        self.assertEqual(len(history), 2)
        self.assertEqual(float(history.iloc[-1]["close"]), 11.5)
        cache_mock.assert_called_once()
        deps_mock.assert_not_called()

    def test_backtest_end_date_can_be_fixed_for_reproducible_cache_runs(self) -> None:
        from backtest_engine import backtest_end_date

        with patch.dict(os.environ, {"STOCK_BACKTEST_END_DATE": "2026-06-18"}):
            self.assertEqual(backtest_end_date().isoformat(), "2026-06-18")

    def test_recent_picks_respects_backtest_end_date(self) -> None:
        class FakeClient:
            def request(self, method, path):
                self.path = path
                return []

        client = FakeClient()

        recent_picks(client, end_date="2026-06-18")

        self.assertIn("scan_date=lte.2026-06-18", client.path)


if __name__ == "__main__":
    unittest.main()

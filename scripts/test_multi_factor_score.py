from pathlib import Path
import sys
import unittest

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "stock_engine"))

import a_stock_trade_common_v7 as common
from a_stock_trade_common_v7 import multi_factor_score, sector_momentum_ranking


def make_price_frame(
    start: float = 10.0,
    step: float = 0.15,
    rows: int = 90,
    volume_base: float = 1000000,
    volume_step: float = 1000,
) -> pd.DataFrame:
    records = []
    for index in range(rows):
        close = start + step * index
        open_price = close - step * 0.35
        high = close + abs(step) * 1.2 + 0.08
        low = open_price - abs(step) * 0.8 - 0.08
        records.append({
            "date": f"2026-01-{(index % 28) + 1:02d}",
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume_base + volume_step * index,
            "turnover": 2.0 + (index % 5) * 0.05,
        })
    return pd.DataFrame(records)


class MultiFactorScoreTest(unittest.TestCase):
    def test_uptrend_stock_gets_high_trend_score(self) -> None:
        result = multi_factor_score(make_price_frame(step=0.18), code="AAPL")

        self.assertGreaterEqual(result["factor_scores"]["trend"], 70)
        self.assertIn("factor_contributions", result)
        self.assertIn("support1", result)

    def test_strong_capital_flow_gets_high_flow_score(self) -> None:
        result = multi_factor_score(
            make_price_frame(),
            code="600001",
            capital_flow={
                "north_bound_net_inflow": 1000,
                "north_bound_holding_change": 0.2,
                "big_order_buy_ratio": 55,
                "main_net_inflow_ratio": 8,
                "margin_balance_change": 100,
            },
        )

        self.assertGreaterEqual(result["factor_scores"]["flow"], 70)

    def test_positive_residual_momentum_scores_above_sixty(self) -> None:
        result = multi_factor_score(
            make_price_frame(start=10, step=0.20),
            code="300001",
            sector_data={
                "sector_return_60d": 5,
                "sector_return_20d_skip_1m": 1,
                "momentum_percentile": 75,
            },
        )

        self.assertGreaterEqual(result["factor_scores"]["momentum"], 60)

    def test_positive_residual_momentum_beats_negative_residual_in_same_sector(self) -> None:
        strong = multi_factor_score(
            make_price_frame(start=10, step=0.18),
            sector_data={"sector_return_60d": 5, "momentum_percentile": 80},
        )
        weak = multi_factor_score(
            make_price_frame(start=20, step=-0.05),
            sector_data={"sector_return_60d": 5, "momentum_percentile": 20},
        )

        self.assertGreater(strong["factor_scores"]["momentum"], weak["factor_scores"]["momentum"])
        self.assertGreater(strong["score"], weak["score"])

    def test_market_regime_changes_total_score(self) -> None:
        frame = make_price_frame(start=10, step=0.12)
        capital_flow = {
            "north_bound_net_inflow": 1000,
            "north_bound_holding_change": 0.2,
            "big_order_buy_ratio": 55,
            "main_net_inflow_ratio": 8,
        }

        bull = multi_factor_score(frame, capital_flow=capital_flow, market_state="强牛市")
        bear = multi_factor_score(frame, capital_flow=capital_flow, market_state="熊市")

        self.assertNotEqual(bull["score"], bear["score"])
        self.assertEqual(set(bull["factor_scores"].keys()), {"trend", "momentum", "volume", "flow", "quality"})

    def test_sector_momentum_ranking_returns_28_deterministic_ranked_sectors(self) -> None:
        snapshot = pd.DataFrame([
            {"code": f"BK{i:04d}", "industry": f"行业{i:02d}", "volume_ratio": 1.0 + i * 0.01}
            for i in range(1, 29)
        ])

        def fake_snapshot() -> pd.DataFrame:
            return snapshot.copy()

        def fake_history(code: str, lookback_days: int = 20) -> pd.DataFrame:
            index = int(code[-2:])
            closes = [100 + day * (0.1 + index * 0.01) for day in range(25)]
            if code == "BK0028":
                closes = [100 + day * 1.5 for day in range(25)]
            return pd.DataFrame({
                "close": closes,
                "volume": [1000 + index * 10 + day for day in range(25)],
            })

        original_snapshot = getattr(common, "_eastmoney_sector_index_snapshot", None)
        original_history = getattr(common, "_eastmoney_sector_index_history", None)
        common._eastmoney_sector_index_snapshot = fake_snapshot
        common._eastmoney_sector_index_history = fake_history
        try:
            first = sector_momentum_ranking(None, lookback_days=20)
            second = sector_momentum_ranking(None, lookback_days=20)
        finally:
            if original_snapshot is not None:
                common._eastmoney_sector_index_snapshot = original_snapshot
            if original_history is not None:
                common._eastmoney_sector_index_history = original_history

        self.assertEqual(len(first), 28)
        self.assertEqual(first, second)
        top_sector = min(first.items(), key=lambda item: item[1]["rank"])
        self.assertEqual(top_sector[0], "行业28")
        self.assertGreater(top_sector[1]["momentum_20d"], 0)
        self.assertIn("avg_volume_ratio", top_sector[1])


if __name__ == "__main__":
    unittest.main()

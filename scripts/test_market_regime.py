from pathlib import Path
import sys
import unittest

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class FakeClient:
    def __init__(self, latest_rows=None):
        self.latest_rows = latest_rows or []
        self.requests = []

    def request(self, method, path, body=None, prefer=None):
        self.requests.append({
            "method": method,
            "path": path,
            "body": body,
            "prefer": prefer,
        })
        if method == "GET" and path.startswith("stock_market_regime?"):
            return self.latest_rows
        return [{"id": 1}]


def csi300_frame(close: float, ma20: float, ma60: float) -> pd.DataFrame:
    closes = [ma60] * 40 + [ma20] * 19 + [close]
    return pd.DataFrame({
        "date": [f"2026-01-{(index % 28) + 1:02d}" for index in range(60)],
        "close": closes,
    })


class MarketRegimeTest(unittest.TestCase):
    def test_strong_bull_regime_wins_first(self) -> None:
        from market_regime import classify_from_metrics

        result = classify_from_metrics(
            csi300_close=4100,
            csi300_ma20=3900,
            csi300_ma60=3800,
            market_turnover_yi=13000,
            limit_up_count=90,
            limit_down_count=2,
            break_rate_pct=20,
            advance_decline_ratio=2.1,
        )

        self.assertEqual(result["regime"], "强牛市")
        self.assertEqual(result["position_cap_pct"], 80)

    def test_range_bound_regime_when_close_near_ma20(self) -> None:
        from market_regime import classify_from_metrics

        result = classify_from_metrics(
            csi300_close=3990,
            csi300_ma20=4000,
            csi300_ma60=3900,
            market_turnover_yi=7000,
            limit_up_count=30,
            limit_down_count=8,
            break_rate_pct=45,
            advance_decline_ratio=1.1,
        )

        self.assertEqual(result["regime"], "震荡市")
        self.assertEqual(result["position_cap_pct"], 40)

    def test_bear_regime_when_below_ma60_with_low_turnover(self) -> None:
        from market_regime import classify_from_metrics

        result = classify_from_metrics(
            csi300_close=3600,
            csi300_ma20=3700,
            csi300_ma60=3800,
            market_turnover_yi=9000,
            limit_up_count=12,
            limit_down_count=20,
            break_rate_pct=55,
            advance_decline_ratio=0.7,
        )

        self.assertEqual(result["regime"], "熊市")
        self.assertEqual(result["position_cap_pct"], 20)

    def test_defense_regime_is_more_specific_than_bear(self) -> None:
        from market_regime import classify_from_metrics

        result = classify_from_metrics(
            csi300_close=3500,
            csi300_ma20=3700,
            csi300_ma60=3800,
            market_turnover_yi=5000,
            limit_up_count=5,
            limit_down_count=55,
            break_rate_pct=60,
            advance_decline_ratio=0.4,
        )

        self.assertEqual(result["regime"], "防御")
        self.assertEqual(result["position_cap_pct"], 10)

    def test_csi300_metrics_from_dataframe(self) -> None:
        from market_regime import csi300_metrics_from_history

        metrics = csi300_metrics_from_history(csi300_frame(close=4100, ma20=3900, ma60=3800))

        self.assertEqual(metrics["csi300_close"], 4100)
        self.assertAlmostEqual(metrics["csi300_ma20"], 3910)
        self.assertAlmostEqual(metrics["csi300_ma60"], 3836.6666666666665)

    def test_non_trading_day_returns_latest_saved_regime(self) -> None:
        from market_regime import latest_saved_regime

        client = FakeClient(latest_rows=[{
            "regime_date": "2026-07-08",
            "regime": "震荡市",
            "csi300_close": 3950.5,
            "market_turnover_yi": 9500,
            "limit_up_count": 65,
            "limit_down_count": 12,
            "break_rate_pct": 22.5,
            "advance_decline_ratio": 1.3,
            "position_cap_pct": 40,
            "details": {"regime_note": "最近交易日状态"},
        }])

        result = latest_saved_regime(client)

        self.assertEqual(result["regime"], "震荡市")
        self.assertEqual(result["regime_note"], "最近交易日状态")

    def test_upsert_market_regime_uses_date_conflict(self) -> None:
        from market_regime import upsert_market_regime

        client = FakeClient()
        upsert_market_regime(client, {
            "regime_date": "2026-07-09",
            "regime": "弱牛市",
            "csi300_close": 4000,
            "csi300_ma20": 3900,
            "csi300_ma60": 3800,
            "market_turnover_yi": 8500,
            "limit_up_count": 50,
            "limit_down_count": 5,
            "break_rate_pct": 30,
            "advance_decline_ratio": 1.4,
            "position_cap_pct": 60,
            "regime_note": "偏多但未到强牛",
        })

        request = client.requests[-1]
        self.assertEqual(request["method"], "POST")
        self.assertIn("stock_market_regime?on_conflict=regime_date", request["path"])
        self.assertIn("resolution=merge-duplicates", request["prefer"])
        self.assertEqual(request["body"][0]["details"]["csi300_ma20"], 3900)

    def test_market_breadth_from_history_rows_uses_cached_change_rate(self) -> None:
        from market_regime import market_breadth_from_history_rows

        result = market_breadth_from_history_rows([
            {"amount": 100000000, "change_rate": 10.1},
            {"amount": 200000000, "change_rate": -10.2},
            {"amount": 300000000, "change_rate": 2.0},
        ])

        self.assertEqual(result["market_turnover_yi"], 6)
        self.assertEqual(result["limit_up_count"], 1)
        self.assertEqual(result["limit_down_count"], 1)
        self.assertEqual(result["advance_decline_ratio"], 2)
        self.assertEqual(result["break_rate_pct"], 50)


if __name__ == "__main__":
    unittest.main()

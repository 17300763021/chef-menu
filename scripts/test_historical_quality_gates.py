from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.historical_contracts import AdjustmentEvent, HistoricalBar
from scripts.market_data.historical_quality_gates import evaluate_historical
from scripts.market_data.quality_gates import accepted
from scripts.market_data.tradeability_contracts import TradeabilityFact


def bar(day: date, raw: str, adjusted: str) -> HistoricalBar:
    return HistoricalBar.build(
        symbol="600519", business_date=day, index_code="000300",
        open_price=Decimal(raw), high=Decimal(raw), low=Decimal(raw), close=Decimal(raw),
        previous_close=None, volume_shares=100, amount_cny=Decimal("1000"), turnover_percent=Decimal("1"),
        qfq_factor=Decimal("1"), hfq_factor=Decimal("1"),
        qfq_prices=(Decimal(adjusted),) * 4, hfq_prices=(Decimal(adjusted),) * 4,
        primary_source="baostock",
    )


def fact(day: date) -> TradeabilityFact:
    return TradeabilityFact(
        symbol="600519", business_date=day, index_code="000300",
        has_primary_bar=True, has_secondary_status=True, is_suspended=False, is_st=False,
        listing_age_sessions=100, limit_rate=Decimal("0.1"), limit_up=None, limit_down=None,
        at_limit_up=False, at_limit_down=False, one_price_limit_up=False, one_price_limit_down=False,
        can_buy=True, can_sell=True, block_reasons=(),
    )


class HistoricalQualityGateTests(unittest.TestCase):
    def test_cross_source_coverage_fails_even_when_returned_prices_match(self) -> None:
        days = (date(2026, 6, 11), date(2026, 6, 12))
        rows = [bar(days[0], "100", "100"), bar(days[1], "95", "100")]
        gates = evaluate_historical(
            expected_keys={("600519", day) for day in days}, calendar_dates=set(days),
            bars=rows, facts=[fact(day) for day in days],
            adjustments=[AdjustmentEvent.build("600519", days[1], "0.95", "1.05")],
            close_checks=[("600519", days[0], Decimal("100"), Decimal("100"))],
            verification_expected=2,
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertFalse(by_name["historical_cross_source_coverage"].passed)
        self.assertTrue(by_name["historical_cross_source_close"].passed)
        self.assertTrue(by_name["corporate_action_adjustment_spot_check"].passed)

    def test_unchanged_factor_metadata_is_not_a_corporate_action(self) -> None:
        days = (date(2026, 6, 11), date(2026, 6, 12), date(2026, 6, 13))
        rows = [bar(days[0], "100", "100"), bar(days[1], "95", "100"), bar(days[2], "101", "101")]
        gates = evaluate_historical(
            expected_keys={("600519", day) for day in days}, calendar_dates=set(days),
            bars=rows, facts=[fact(day) for day in days],
            adjustments=[
                AdjustmentEvent.build("600519", days[1], "0.95", "1.05"),
                AdjustmentEvent.build("600519", days[2], "0.95", "1.05"),
            ],
            close_checks=[("600519", day, Decimal("100"), Decimal("100")) for day in days],
            verification_expected=3,
        )
        gate = next(value for value in gates if value.name == "corporate_action_adjustment_spot_check")
        self.assertTrue(gate.passed)
        self.assertEqual(gate.actual, "1/1 (100.00%)")

    def test_shard_defers_empty_cross_source_sample_to_merge(self) -> None:
        days = (date(2026, 6, 11), date(2026, 6, 12))
        rows = [bar(days[0], "100", "100"), bar(days[1], "95", "100")]
        gates = evaluate_historical(
            expected_keys={("600519", day) for day in days}, calendar_dates=set(days),
            bars=rows, facts=[fact(day) for day in days],
            adjustments=[AdjustmentEvent.build("600519", days[1], "0.95", "1.05")],
            close_checks=[], verification_expected=0, cross_source_critical=False,
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertFalse(by_name["historical_cross_source_coverage"].passed)
        self.assertFalse(by_name["historical_cross_source_coverage"].critical)
        self.assertTrue(accepted(gates))


if __name__ == "__main__":
    unittest.main()

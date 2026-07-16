from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.tradeability import derive_tradeability
from scripts.market_data.tradeability_contracts import limit_rate, rounded_limit


class TradeabilityTests(unittest.TestCase):
    def test_board_and_st_limit_rates(self) -> None:
        self.assertEqual(limit_rate("600519", date(2026, 1, 1), False, 100), Decimal("0.10"))
        self.assertEqual(limit_rate("688001", date(2026, 1, 1), False, 100), Decimal("0.20"))
        self.assertEqual(limit_rate("300001", date(2020, 8, 21), False, 100), Decimal("0.10"))
        self.assertEqual(limit_rate("300001", date(2020, 8, 24), False, 100), Decimal("0.20"))
        self.assertEqual(limit_rate("600519", date(2026, 1, 1), True, 100), Decimal("0.05"))
        self.assertIsNone(limit_rate("600519", date(2026, 1, 1), False, 4))

    def test_limit_rounding_uses_cent_tick(self) -> None:
        self.assertEqual(rounded_limit(Decimal("10.05"), Decimal("0.10"), 1), Decimal("11.06"))
        self.assertEqual(rounded_limit(Decimal("10.05"), Decimal("0.10"), -1), Decimal("9.05"))

    def test_missing_status_blocks_both_sides(self) -> None:
        fact = derive_tradeability(
            symbol="600519", business_date=date(2026, 1, 2), index_code="000300", listing_age_sessions=100,
            primary={"high": Decimal("10"), "low": Decimal("9"), "close": Decimal("9.5")}, secondary=None,
        )
        self.assertFalse(fact.can_buy)
        self.assertFalse(fact.can_sell)
        self.assertIn("missing_secondary_status", fact.block_reasons)

    def test_one_price_limit_up_blocks_buy_only(self) -> None:
        fact = derive_tradeability(
            symbol="600519", business_date=date(2026, 1, 2), index_code="000300", listing_age_sessions=100,
            primary={"high": Decimal("11"), "low": Decimal("11"), "close": Decimal("11")},
            secondary={"tradestatus": "1", "isST": "0", "preclose": "10"},
        )
        self.assertFalse(fact.can_buy)
        self.assertTrue(fact.can_sell)

    def test_one_price_limit_down_blocks_sell_only(self) -> None:
        fact = derive_tradeability(
            symbol="600519", business_date=date(2026, 1, 2), index_code="000300", listing_age_sessions=100,
            primary={"high": Decimal("9"), "low": Decimal("9"), "close": Decimal("9")},
            secondary={"tradestatus": "1", "isST": "0", "preclose": "10"},
        )
        self.assertTrue(fact.can_buy)
        self.assertFalse(fact.can_sell)


if __name__ == "__main__":
    unittest.main()

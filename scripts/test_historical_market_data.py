from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.historical_contracts import HistoricalBar, SecurityReference
from scripts.market_data.historical_bars import verification_symbols


class HistoricalMarketDataTests(unittest.TestCase):
    def test_adjusted_prices_are_derived_without_overwriting_raw(self) -> None:
        row = HistoricalBar.build(
            symbol="600519", business_date=date(2026, 7, 15), index_code="000300",
            open_price=Decimal("100"), high=Decimal("110"), low=Decimal("90"), close=Decimal("105"),
            previous_close=Decimal("99"), volume_shares=100, amount_cny=Decimal("10000"),
            turnover_percent=Decimal("1"), qfq_factor=Decimal("0.5"), hfq_factor=Decimal("2"),
        )
        self.assertEqual(row.close, Decimal("105"))
        self.assertEqual(row.qfq_close, Decimal("52.5000"))
        self.assertEqual(row.hfq_close, Decimal("210.0000"))

    def test_security_reference_rejects_invalid_symbol(self) -> None:
        with self.assertRaises(ValueError):
            SecurityReference.build("AAPL", "Apple", date(2020, 1, 1))

    def test_full_verification_sample_is_bounded_and_deterministic(self) -> None:
        symbols = [f"{value:06d}" for value in range(100)]
        first = verification_symbols(symbols, "full", maximum=40)
        self.assertEqual(first, verification_symbols(symbols, "full", maximum=40))
        self.assertEqual(len(first), 40)
        self.assertEqual(first[0], symbols[0])
        self.assertEqual(first[-1], symbols[-1])


if __name__ == "__main__":
    unittest.main()

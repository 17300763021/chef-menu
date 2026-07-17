from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.historical_contracts import HistoricalBar, SecurityReference
from scripts.market_data.historical_bars import bounded_symbols, shard_symbols, verification_symbols
from scripts.market_data.sources.baostock_history_source import BaostockHistorySource


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

    def test_bounded_and_round_robin_shards_cover_once(self) -> None:
        symbols = [f"{value:06d}" for value in range(1403)]
        bounded = bounded_symbols(symbols, 100)
        self.assertEqual(len(bounded), 100)
        shards = [shard_symbols(bounded, index, 2) for index in range(2)]
        self.assertEqual(sorted([item for shard in shards for item in shard]), bounded)
        self.assertFalse(set(shards[0]) & set(shards[1]))

    def test_global_verification_targets_partition_without_loss(self) -> None:
        symbols = [f"{value:06d}" for value in range(100)]
        targets = verification_symbols(symbols, "full", maximum=40)
        target_set = set(targets)
        partitions = [
            [symbol for symbol in targets if symbol in set(shard_symbols(symbols, index, 10))]
            for index in range(10)
        ]
        self.assertEqual(sorted(symbol for partition in partitions for symbol in partition), targets)
        self.assertEqual(sum(len(partition) for partition in partitions), len(target_set))

    def test_adjusted_prices_do_not_require_volume_or_amount(self) -> None:
        rows = [{
            "date": "2026-07-15", "open": "10", "high": "11", "low": "9", "close": "10.5",
            "volume": "", "amount": "",
        }]
        prices = BaostockHistorySource.adjusted_prices_from_rows(rows)
        self.assertEqual(prices[date(2026, 7, 15)][3], Decimal("10.5000"))

    def test_suspended_status_row_is_not_a_trading_bar(self) -> None:
        rows = {date(2026, 7, 15): {
            "date": "2026-07-15", "code": "sh.600519",
            "open": "10", "high": "10", "low": "10", "close": "10", "preclose": "10",
            "volume": "", "amount": "", "turn": "", "tradestatus": "0", "isST": "0",
        }}
        self.assertEqual(BaostockHistorySource.bars_from_status("600519", rows), {})


if __name__ == "__main__":
    unittest.main()

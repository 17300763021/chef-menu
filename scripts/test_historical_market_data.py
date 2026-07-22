from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import DailyBar
from scripts.market_data.historical_contracts import HistoricalBar, SecurityReference
from scripts.market_data.historical_bars import build_plan, bounded_symbols, current_universe_from_canonical, load_calendars, shard_symbols, verification_symbols
from scripts.market_data.sources.akshare_history_source import AkshareHistorySource
from scripts.market_data.sources.baostock_history_source import BaostockHistorySource
from scripts.market_data.universe_contracts import CurrentUniverse


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

    def test_current_universe_canonical_roundtrip(self) -> None:
        current = CurrentUniverse(
            as_of_date=date(2026, 7, 16),
            members={"000300": ("000001",), "000905": ("600519",)},
            source_urls={"000300": "https://example.test/300.xls", "000905": "https://example.test/500.xls"},
            source_hashes={"000300": "a" * 64, "000905": "b" * 64},
        )
        self.assertEqual(current_universe_from_canonical(current.canonical()), current)

    def test_frozen_calendars_skip_live_calendar_sources(self) -> None:
        frozen_primary = TradingCalendar.build("akshare_calendar", date(2026, 7, 1), date(2026, 7, 3), [date(2026, 7, 1), date(2026, 7, 2)])
        frozen_secondary = TradingCalendar.build("baostock_calendar", date(2026, 7, 1), date(2026, 7, 3), [date(2026, 7, 1), date(2026, 7, 2)])

        class FailingCalendarSource:
            def fetch(self, start: date, end: date) -> TradingCalendar:
                raise AssertionError("frozen shard should not fetch live calendars")

        with (
            patch("scripts.market_data.historical_bars.AkshareCalendarSource", FailingCalendarSource),
            patch("scripts.market_data.historical_bars.BaostockCalendarSource", FailingCalendarSource),
        ):
            primary, secondary, gates, _source = load_calendars(
                date(2026, 7, 3),
                primary_calendar=frozen_primary,
                secondary_calendar=frozen_secondary,
            )
        self.assertEqual(primary, frozen_primary)
        self.assertEqual(secondary, frozen_secondary)
        self.assertTrue(all(gate.passed for gate in gates))

    def test_preflight_plan_freezes_current_snapshot_for_shards(self) -> None:
        class FakeCalendar:
            @staticmethod
            def build(source: str) -> TradingCalendar:
                return TradingCalendar.build(source, date(2026, 7, 1), date(2026, 7, 16), tuple(date(2026, 7, day) for day in range(1, 17)))

        class FakeCalendarSource:
            def fetch(self, start: date, end: date) -> TradingCalendar:
                return FakeCalendar.build("akshare_calendar")

        class FakeBaostockCalendarSource:
            def fetch(self, start: date, end: date) -> TradingCalendar:
                return FakeCalendar.build("baostock_calendar")

        class FakeCsiSource:
            def fetch_current(self) -> CurrentUniverse:
                return CurrentUniverse(
                    as_of_date=date(2026, 7, 16),
                    members={
                        "000300": tuple(f"{value:06d}" for value in range(60)),
                        "000905": tuple(f"{value:06d}" for value in range(60, 120)),
                    },
                    source_urls={"000300": "https://example.test/300.xls", "000905": "https://example.test/500.xls"},
                    source_hashes={"000300": "a" * 64, "000905": "b" * 64},
                )

            def fetch_events(self, calendar: FakeCalendar, through: date):
                raise AssertionError("plan should not download historical CSI attachments")

            def fetch_indexed_events(self, through: date, discovered_notice_ids=None):
                return [], {11518}, {"accepted_manifest_event_sha256": "fixture"}

        with (
            patch("scripts.market_data.historical_bars.AkshareCalendarSource", FakeCalendarSource),
            patch("scripts.market_data.historical_bars.BaostockCalendarSource", FakeBaostockCalendarSource),
            patch("scripts.market_data.historical_bars.CsiIndexSource", FakeCsiSource),
        ):
            plan = build_plan(date(2026, 7, 16), "preflight")
        self.assertEqual(plan["shard_count"], 10)
        self.assertEqual(plan["symbol_count"], 100)
        self.assertEqual(plan["current_snapshot"]["as_of_date"], "2026-07-16")
        self.assertEqual(plan["current_snapshot"]["source_hashes"]["000300"], "a" * 64)
        self.assertEqual(plan["primary_calendar"]["source"], "akshare_calendar")
        self.assertEqual(plan["secondary_calendar"]["source"], "baostock_calendar")
        self.assertNotEqual(plan["calendar_source"]["primary_calendar_sha256"], plan["calendar_source"]["secondary_calendar_sha256"])
        self.assertEqual(plan["csi_discovered_notice_ids"], [11518])
        self.assertEqual(plan["csi_event_index_source"]["accepted_manifest_event_sha256"], "fixture")

    def test_akshare_history_falls_back_to_eastmoney_when_sina_is_empty(self) -> None:
        fallback_row = DailyBar(
            source="akshare_eastmoney", symbol="000413", exchange="SZSE",
            business_date=date(2024, 8, 14), open=Decimal("1"), high=Decimal("1"),
            low=Decimal("1"), close=Decimal("1"), previous_close=None,
            volume_shares=100, amount_cny=Decimal("100"), turnover_percent=Decimal("1"),
            trade_status="trading", is_st=None,
        )

        class FakeEastmoneySource:
            def __init__(self, timeout_seconds: float, attempts: int) -> None:
                self.timeout_seconds = timeout_seconds
                self.attempts = attempts

            def fetch(self, symbol: str, start: date, end: date):
                return [fallback_row]

        with (
            patch.object(AkshareHistorySource, "_frame", side_effect=RuntimeError("Sina returned no raw rows")),
            patch("scripts.market_data.sources.akshare_source.AkshareSource", FakeEastmoneySource),
        ):
            rows = AkshareHistorySource(attempts=2).fetch_raw("000413", date(2024, 8, 1), date(2024, 8, 14))
        self.assertEqual(rows, [fallback_row])
        self.assertEqual(rows[0].source, "akshare_eastmoney")


if __name__ == "__main__":
    unittest.main()

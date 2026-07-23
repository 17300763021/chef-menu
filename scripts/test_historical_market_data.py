from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import DailyBar
from scripts.market_data.historical_contracts import HistoricalBar, SecurityReference
from scripts.market_data.historical_bars import build_plan, bounded_symbols, current_universe_from_canonical, history_stagger_seconds, load_calendars, run, shard_symbols, verification_symbols
from scripts.market_data.sources.akshare_history_source import AkshareEastmoneyHistorySource, AkshareHistorySource
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

    def test_history_stagger_applies_only_to_full_mode(self) -> None:
        self.assertEqual(history_stagger_seconds("preflight", 5), 0)
        self.assertEqual(history_stagger_seconds("sample", 5), 0)
        self.assertEqual(history_stagger_seconds("full", 0), 0)
        self.assertEqual(history_stagger_seconds("full", 5), 50)
        self.assertEqual(history_stagger_seconds("full", 6), 0)

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

    def test_eastmoney_adjustment_events_are_derived_from_adjusted_close(self) -> None:
        source = AkshareEastmoneyHistorySource()
        raw = {
            date(2026, 7, 20): DailyBar(
                source="akshare_eastmoney", symbol="600519", exchange="SSE",
                business_date=date(2026, 7, 20), open=Decimal("10"), high=Decimal("11"),
                low=Decimal("9"), close=Decimal("10"), previous_close=None,
                volume_shares=100, amount_cny=Decimal("1000"), turnover_percent=Decimal("1"),
                trade_status="trading", is_st=None,
            ),
            date(2026, 7, 21): DailyBar(
                source="akshare_eastmoney", symbol="600519", exchange="SSE",
                business_date=date(2026, 7, 21), open=Decimal("20"), high=Decimal("21"),
                low=Decimal("19"), close=Decimal("20"), previous_close=None,
                volume_shares=100, amount_cny=Decimal("2000"), turnover_percent=Decimal("1"),
                trade_status="trading", is_st=None,
            ),
        }
        qfq = {
            date(2026, 7, 20): (Decimal("5"), Decimal("5.5"), Decimal("4.5"), Decimal("5")),
            date(2026, 7, 21): (Decimal("20"), Decimal("21"), Decimal("19"), Decimal("20")),
        }
        hfq = {
            date(2026, 7, 20): (Decimal("20"), Decimal("22"), Decimal("18"), Decimal("20")),
            date(2026, 7, 21): (Decimal("40"), Decimal("42"), Decimal("38"), Decimal("40")),
        }

        events = source.derive_adjustments("600519", raw, qfq, hfq)

        self.assertEqual([event.effective_date for event in events], [date(2026, 7, 20), date(2026, 7, 21)])
        self.assertEqual(events[0].qfq_factor, Decimal("0.500000"))
        self.assertEqual(events[0].hfq_factor, Decimal("2.000000"))
        self.assertEqual(events[0].source, "akshare_eastmoney_derived")

    def test_primary_history_run_does_not_require_baostock_history_login(self) -> None:
        calendar = TradingCalendar.build(
            "akshare_calendar", date(2026, 7, 20), date(2026, 7, 22),
            [date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22)],
        )
        current = CurrentUniverse(
            as_of_date=date(2026, 7, 22),
            members={"000300": ("600519",), "000905": ()},
            source_urls={"000300": "https://example.test/300.xls", "000905": "https://example.test/500.xls"},
            source_hashes={"000300": "a" * 64, "000905": "b" * 64},
        )

        class FakeCsiSource:
            def fetch_current(self) -> CurrentUniverse:
                raise AssertionError("frozen current universe should be used")

            def fetch_indexed_events(self, through: date, discovered_notice_ids=None):
                return [], set(), {"accepted_manifest_event_sha256": "fixture"}

        class FakePrimarySource:
            def __init__(self, timeout_seconds: float = 30.0, attempts: int = 5) -> None:
                pass

            @staticmethod
            def _factor(adjusted_close: Decimal, raw_close: Decimal) -> Decimal:
                return (adjusted_close / raw_close).quantize(Decimal("0.000001"))

            def fetch_raw(self, symbol: str, start: date, end: date):
                return [
                    DailyBar(
                        source="akshare_eastmoney", symbol="600519", exchange="SSE",
                        business_date=business_date, open=Decimal("10"), high=Decimal("11"),
                        low=Decimal("9"), close=Decimal("10"), previous_close=None,
                        volume_shares=100, amount_cny=Decimal("1000"), turnover_percent=Decimal("1"),
                        trade_status="trading", is_st=None,
                    )
                    for business_date in calendar.open_dates
                ]

            def fetch_adjusted_prices(self, symbol: str, start: date, end: date, adjust: str):
                factor = Decimal("0.5") if adjust == "qfq" else Decimal("2")
                return {
                    business_date: (Decimal("10") * factor, Decimal("11") * factor, Decimal("9") * factor, Decimal("10") * factor)
                    for business_date in calendar.open_dates
                }

            def derive_adjustments(self, symbol: str, raw, qfq, hfq):
                from scripts.market_data.historical_contracts import AdjustmentEvent

                return [AdjustmentEvent.build(symbol, date(2026, 7, 20), "0.5", "2")]

            def build_reference(self, symbol: str, rows):
                return SecurityReference.build(symbol, symbol, date(2026, 7, 20))

            def build_status_from_raw(self, rows):
                previous_close = ""
                result = {}
                for business_date in sorted(rows):
                    result[business_date] = {"tradestatus": "1", "isST": "0", "preclose": previous_close}
                    previous_close = "10"
                return result

            def fetch_bundle(self, symbol: str, start: date, end: date):
                raw = {row.business_date: row for row in self.fetch_raw(symbol, start, end)}
                qfq = self.fetch_adjusted_prices(symbol, start, end, "qfq")
                hfq = self.fetch_adjusted_prices(symbol, start, end, "hfq")
                return (
                    raw,
                    qfq,
                    hfq,
                    self.derive_adjustments(symbol, raw, qfq, hfq),
                    self.build_reference(symbol, raw),
                    self.build_status_from_raw(raw),
                    "akshare_eastmoney",
                )

        with (
            patch("scripts.market_data.historical_bars.CsiIndexSource", FakeCsiSource),
            patch("scripts.market_data.historical_bars.evaluate_universe", return_value=[]),
            patch("scripts.market_data.historical_bars.AkshareEastmoneyHistorySource", FakePrimarySource),
            patch("scripts.market_data.historical_bars.fetch_primary", return_value=({"600519": list(FakePrimarySource().fetch_raw("600519", date(2026, 7, 20), date(2026, 7, 22)))}, {})),
            patch("scripts.market_data.sources.baostock_history_source.BaostockHistorySource.__enter__", side_effect=AssertionError("BaoStock history must not be opened")),
        ):
            manifest, bars, facts, adjustments, references, close_checks = run(
                date(2026, 7, 22),
                mode="sample",
                current_universe=current,
                primary_calendar=calendar,
                secondary_calendar=calendar,
            )

        self.assertEqual(manifest["primary_source"], "akshare_historical_bundle")
        self.assertEqual(manifest["primary_sources_by_symbol"], {"600519": "akshare_eastmoney"})
        self.assertFalse(manifest["simulation_orders_allowed"])
        self.assertEqual(len(bars), 3)
        self.assertEqual({bar.primary_source for bar in bars}, {"akshare_eastmoney"})
        self.assertEqual(len(facts), 3)
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(len(references), 1)
        self.assertEqual(len(close_checks), 3)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import json
import unittest
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import DailyBar
from scripts.market_data.adjustment_engine import build_adjusted_series_from_factor_events
from scripts.market_data.historical_contracts import AdjustmentEvent, HistoricalBar, SecurityReference
from scripts.market_data.historical_bars import SymbolDeadlineInterrupt, build_plan, bounded_symbols, current_universe_from_canonical, enrich_repair_plan, fetch_bundle_from_live_or_archive, fetch_primary, frozen_archive_source, history_stagger_seconds, load_calendars, run, shard_symbols, verification_symbols
from scripts.market_data.manifest import sha256
from scripts.market_data.sources.akshare_history_source import AkshareEastmoneyHistorySource, AkshareHistorySource
from scripts.market_data.sources.baostock_history_source import BaostockHistorySource
from scripts.market_data.sources.frozen_archive_history_source import (
    ARCHIVE_BUSINESS_END,
    ARCHIVE_PATH,
    ARCHIVE_SCHEMA_VERSION,
    FACTOR_SOURCE,
    PRIMARY_SOURCE,
    STATUS_ONLY_SOURCE,
    VERIFICATION_SOURCE,
    FrozenArchiveHistorySource,
    validate_archive_document,
)
from scripts.market_data.sources.tencent_history_source import TencentHistorySource, TencentIndexCalendarSource
from scripts.market_data.tidb_checkpoint_store import HistoricalEvidence
from scripts.market_data.universe_contracts import CurrentUniverse


class HistoricalMarketDataTests(unittest.TestCase):
    @staticmethod
    def _daily_bar(business_date: date, close: str, source: str = "akshare_eastmoney") -> DailyBar:
        return DailyBar(
            source=source, symbol="000001", exchange="SZSE",
            business_date=business_date, open=Decimal(close), high=Decimal(close),
            low=Decimal(close), close=Decimal(close), previous_close=None,
            volume_shares=100, amount_cny=Decimal("1000"),
            turnover_percent=Decimal("0.1"), trade_status="trading", is_st=None,
        )

    def test_daily_eastmoney_reference_uses_reported_change_amount(self) -> None:
        target = date(2026, 7, 27)
        bar = self._daily_bar(target, "11.11")

        class Frame:
            columns = [f"column-{index}" for index in range(10)] + ["涨跌额"]

            @staticmethod
            def to_dict(*, orient: str):
                self.assertEqual(orient, "records")
                return [{"涨跌额": "0.01"}]

        source = AkshareEastmoneyHistorySource(attempts=1)
        with (
            patch.object(source, "_frame", return_value=Frame()),
            patch(
                "scripts.market_data.sources.akshare_history_source.normalize_akshare_row",
                return_value=bar,
            ),
        ):
            rows, source_name, reported, reference_source = source.fetch_daily_raw_with_reference(
                "000001", date(2026, 7, 24), target,
            )
        self.assertEqual(rows, {target: bar})
        self.assertEqual(source_name, "akshare_eastmoney")
        self.assertEqual(reported, Decimal("11.1000"))
        self.assertEqual(reference_source, "akshare_eastmoney_change_amount")

    def test_daily_reference_falls_back_to_exact_sina_predecessor(self) -> None:
        previous = date(2026, 7, 24)
        target = date(2026, 7, 27)
        predecessor = self._daily_bar(previous, "10.00", "akshare_sina")
        target_bar = self._daily_bar(target, "10.20", "akshare_sina")
        source = AkshareEastmoneyHistorySource(attempts=1)
        with (
            patch.object(source, "_frame", side_effect=RuntimeError("Eastmoney offline")),
            patch.object(source, "_sina_frame", return_value=object()),
            patch.object(source, "_sina_raw_bars", return_value=[predecessor, target_bar]),
        ):
            rows, source_name, reported, reference_source = source.fetch_daily_raw_with_reference(
                "000001", previous, target,
            )
        self.assertEqual(rows, {previous: predecessor, target: target_bar})
        self.assertEqual(source_name, "akshare_sina")
        self.assertEqual(reported, Decimal("10.00"))
        self.assertEqual(reference_source, "akshare_sina_exact_predecessor_close")

    def test_frozen_archive_is_bounded_dual_source_and_hash_verified(self) -> None:
        source = FrozenArchiveHistorySource()
        self.assertEqual(source.document["schema_version"], ARCHIVE_SCHEMA_VERSION)
        self.assertFalse(source.document["authoritative"])
        self.assertFalse(source.document["simulation_orders_allowed"])
        expected_counts = {"000939": 231, "002005": 1954, "600485": 188}
        expected_out_dates = {
            "000939": date(2020, 12, 17),
            "002005": None,
            "600485": date(2021, 6, 1),
        }
        for symbol, expected_count in expected_counts.items():
            raw, qfq, hfq, events, reference, status, primary_source, factor_source = source.fetch_bundle(
                symbol, date(2018, 1, 1), ARCHIVE_BUSINESS_END,
            )
            verification = source.fetch_verification(symbol, date(2018, 1, 1), ARCHIVE_BUSINESS_END)
            self.assertEqual(len(raw), expected_count)
            self.assertEqual(len(verification), expected_count)
            self.assertEqual(set(raw), {row.business_date for row in verification})
            self.assertTrue(set(raw).issubset(status))
            self.assertEqual(primary_source, PRIMARY_SOURCE)
            self.assertEqual(factor_source, FACTOR_SOURCE)
            self.assertEqual({row.source for row in verification}, {VERIFICATION_SOURCE})
            self.assertNotEqual(primary_source, VERIFICATION_SOURCE)
            self.assertTrue(events)
            self.assertEqual(reference.out_date, expected_out_dates[symbol])
            self.assertTrue(all(min(*qfq[key], *hfq[key]) > 0 for key in raw))

    def test_frozen_archive_rejects_future_or_unknown_scope(self) -> None:
        source = FrozenArchiveHistorySource()
        self.assertFalse(source.supports("000939", date(2018, 1, 1), date(2026, 7, 25)))
        self.assertFalse(source.supports("600519", date(2018, 1, 1), ARCHIVE_BUSINESS_END))
        with self.assertRaisesRegex(RuntimeError, "scope rejected"):
            source.fetch_bundle("002005", date(2026, 7, 24), date(2026, 7, 25))

    def test_frozen_archive_preserves_confirmed_all_session_suspension(self) -> None:
        source = FrozenArchiveHistorySource()
        for symbol in ("000939", "002005", "600485"):
            raw, qfq, hfq, _events, _reference, status, primary_source, factor_source = source.fetch_bundle(
                symbol, date(2018, 1, 2), date(2018, 6, 8),
            )
            self.assertEqual(raw, {})
            self.assertEqual(qfq, {})
            self.assertEqual(hfq, {})
            self.assertEqual(len(status), 105)
            self.assertTrue(all(row["tradestatus"] == "0" for row in status.values()))
            self.assertEqual(primary_source, STATUS_ONLY_SOURCE)
            self.assertEqual(factor_source, FACTOR_SOURCE)

    def test_frozen_archive_rejects_tampering_even_when_hashes_are_recomputed(self) -> None:
        document = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
        tampered = copy.deepcopy(document)
        payload = tampered["symbols"]["000939"]
        payload["primary_rows"][0]["close"] = "999.0000"
        payload["content_sha256"] = sha256({
            key: value for key, value in payload.items() if key != "content_sha256"
        })
        tampered["dataset_sha256"] = sha256({
            key: value for key, value in tampered.items() if key != "dataset_sha256"
        })
        with self.assertRaisesRegex(ValueError, "archive OHLC mismatch"):
            validate_archive_document(tampered)

    def test_verification_and_primary_use_archive_only_after_live_failure(self) -> None:
        frozen_archive_source.cache_clear()
        ranges = {"000939": (date(2018, 7, 2), date(2020, 12, 16))}
        with patch.object(AkshareHistorySource, "fetch_raw", side_effect=RuntimeError("cloud endpoints empty")):
            verification, failures = fetch_primary(["000939"], ranges, workers=1)
        self.assertEqual(failures, {})
        self.assertEqual(len(verification["000939"]), 231)
        self.assertEqual({row.source for row in verification["000939"]}, {VERIFICATION_SOURCE})

        live = AkshareEastmoneyHistorySource(attempts=1)
        with patch.object(live, "fetch_bundle", side_effect=RuntimeError("cloud endpoints empty")):
            bundle = fetch_bundle_from_live_or_archive(
                live, "000939", date(2018, 7, 2), date(2020, 12, 16),
            )
        self.assertEqual(len(bundle[0]), 231)
        self.assertEqual(bundle[6], PRIMARY_SOURCE)
        self.assertEqual(bundle[7], FACTOR_SOURCE)

    def test_symbol_deadline_interrupt_cannot_be_swallowed_by_vendor_exception_handler(self) -> None:
        self.assertTrue(issubclass(SymbolDeadlineInterrupt, BaseException))
        self.assertFalse(issubclass(SymbolDeadlineInterrupt, Exception))

        @contextmanager
        def immediate_deadline(_seconds: int):
            raise SymbolDeadlineInterrupt("fixture deadline")
            yield

        with patch("scripts.market_data.historical_bars.symbol_deadline", immediate_deadline):
            rows, failures = fetch_primary(
                ["000001"],
                {"000001": (date(2026, 7, 20), date(2026, 7, 21))},
                workers=1,
            )
        self.assertEqual(rows, {})
        self.assertEqual(failures, {"000001": "TimeoutError: fixture deadline"})

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
            patch("scripts.market_data.historical_bars.TencentIndexCalendarSource", FailingCalendarSource),
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

        class FakeTencentCalendarSource:
            def fetch(self, start: date, end: date) -> TradingCalendar:
                return FakeCalendar.build("tencent_sse_index_calendar")

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
            patch("scripts.market_data.historical_bars.TencentIndexCalendarSource", FakeTencentCalendarSource),
            patch("scripts.market_data.historical_bars.CsiIndexSource", FakeCsiSource),
        ):
            plan = build_plan(date(2026, 7, 16), "preflight")
            repeated_plan = build_plan(date(2026, 7, 16), "preflight")
        self.assertEqual(plan["shard_count"], 10)
        self.assertEqual(plan["symbol_count"], 100)
        self.assertEqual(plan["current_snapshot"]["as_of_date"], "2026-07-16")
        self.assertEqual(plan["current_snapshot"]["source_hashes"]["000300"], "a" * 64)
        self.assertEqual(plan["primary_calendar"]["source"], "akshare_calendar")
        self.assertEqual(
            plan["secondary_calendar"]["source"],
            "tencent_sse_index_calendar",
        )
        self.assertNotEqual(plan["calendar_source"]["primary_calendar_sha256"], plan["calendar_source"]["secondary_calendar_sha256"])
        self.assertEqual(plan["csi_discovered_notice_ids"], [11518])
        self.assertEqual(plan["csi_event_index_source"]["accepted_manifest_event_sha256"], "fixture")
        self.assertEqual(len(plan["checkpoint_scope_sha256"]), 64)
        self.assertEqual(plan["checkpoint_scope_sha256"], repeated_plan["checkpoint_scope_sha256"])

    def test_quality_repair_plan_can_be_reenriched_idempotently(self) -> None:
        base = {
            "mode": "full",
            "business_end": "2026-07-24",
            "symbol_count": 1,
            "shard_size": 10,
            "shard_count": 1,
            "matrix": {"include": [{"shard_index": 0, "shard_count": 1}]},
        }
        base["checkpoint_scope_sha256"] = sha256(base)
        repair = {
            "repair_matrix": {"include": []},
            "repair_shard_count": 0,
            "repair_symbol_count": 0,
            "resumable_symbol_count": 1,
            "repair_details": [],
        }

        class Connection:
            def close(self) -> None:
                pass

        with (
            patch("scripts.market_data.historical_bars.checkpoint_expectations", return_value={0: {"000001": (1, False, "2026-07-24", "2026-07-24")}}),
            patch("scripts.market_data.tidb_checkpoint_store.TiDBConfig.from_env", return_value=object()),
            patch("scripts.market_data.tidb_checkpoint_store.connect", return_value=Connection()),
            patch("scripts.market_data.tidb_checkpoint_store.ensure_schema"),
            patch("scripts.market_data.tidb_checkpoint_store.build_checkpoint_repair_plan", return_value=repair),
        ):
            first = enrich_repair_plan(base)
            second = enrich_repair_plan(first)
        self.assertEqual(first, second)

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

    def test_factor_events_create_positive_auditable_adjusted_series(self) -> None:
        raw = {
            date(2026, 7, 20): DailyBar(
                source="tencent_archive", symbol="000937", exchange="SZSE",
                business_date=date(2026, 7, 20), open=Decimal("10"), high=Decimal("11"),
                low=Decimal("9"), close=Decimal("10"), previous_close=None,
                volume_shares=100, amount_cny=Decimal("1000"), turnover_percent=Decimal("1"),
                trade_status="trading", is_st=None,
            ),
            date(2026, 7, 21): DailyBar(
                source="tencent_archive", symbol="000937", exchange="SZSE",
                business_date=date(2026, 7, 21), open=Decimal("20"), high=Decimal("21"),
                low=Decimal("19"), close=Decimal("20"), previous_close=None,
                volume_shares=100, amount_cny=Decimal("2000"), turnover_percent=Decimal("1"),
                trade_status="trading", is_st=None,
            ),
        }
        vendor_events = [
            AdjustmentEvent("000937", date(1900, 1, 1), Decimal("2"), Decimal("1"), source="sina"),
            AdjustmentEvent("000937", date(2026, 7, 21), Decimal("1"), Decimal("2"), source="sina"),
        ]
        qfq, hfq, events = build_adjusted_series_from_factor_events(
            "000937", raw, vendor_events, source="akshare_sina_factor_multiplicative",
        )
        self.assertEqual(qfq[date(2026, 7, 20)][3], Decimal("5.0000"))
        self.assertEqual(qfq[date(2026, 7, 21)][3], Decimal("20.0000"))
        self.assertEqual(hfq[date(2026, 7, 20)][3], Decimal("10.0000"))
        self.assertEqual(hfq[date(2026, 7, 21)][3], Decimal("40.0000"))
        self.assertEqual([event.effective_date for event in events], [date(1900, 1, 1), date(2026, 7, 21)])
        self.assertEqual(events[0].qfq_factor, Decimal("0.500000"))
        self.assertTrue(all(event.source == "akshare_sina_factor_multiplicative" for event in events))

    def test_tencent_archive_parser_preserves_volume_and_amount_units(self) -> None:
        source = TencentHistorySource(attempts=1)
        row = [
            "2024-04-25", "0.41", "0.42", "0.43", "0.40", "86788.50",
            {}, "1.25", "355.83", "0.00", "0.00",
        ]
        with patch.object(TencentHistorySource, "_rows", return_value=[row]):
            bars = source.fetch_raw("002699", date(2024, 4, 25), date(2024, 4, 25))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].source, "tencent_archive")
        self.assertEqual(bars[0].volume_shares, 8_678_850)
        self.assertEqual(bars[0].amount_cny, Decimal("3558300.00"))
        self.assertEqual(bars[0].turnover_percent, Decimal("1.250000"))

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
                    "akshare_sina_factor_multiplicative",
                )

        def verification_rows():
            return [
                DailyBar(
                    source="akshare_sina", symbol=row.symbol, exchange=row.exchange,
                    business_date=row.business_date, open=row.open, high=row.high,
                    low=row.low, close=row.close, previous_close=row.previous_close,
                    volume_shares=row.volume_shares, amount_cny=row.amount_cny,
                    turnover_percent=row.turnover_percent, trade_status=row.trade_status,
                    is_st=row.is_st,
                )
                for row in FakePrimarySource().fetch_raw(
                    "600519", date(2026, 7, 20), date(2026, 7, 22),
                )
            ]

        with (
            patch("scripts.market_data.historical_bars.CsiIndexSource", FakeCsiSource),
            patch("scripts.market_data.historical_bars.evaluate_universe", return_value=[]),
            patch("scripts.market_data.historical_bars.AkshareEastmoneyHistorySource", FakePrimarySource),
            patch("scripts.market_data.historical_bars.fetch_primary", return_value=({"600519": verification_rows()}, {})),
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

        resume_evidence = HistoricalEvidence(
            manifest={"resumed_symbols": ["600519"]},
            bars=[row.canonical() for row in bars],
            tradeability=[row.canonical() for row in facts],
            adjustments=[row.canonical() for row in adjustments],
            references=[row.canonical() for row in references],
            verification_checks=[{
                "symbol": symbol, "business_date": business_date.isoformat(),
                "primary_close": format(primary, "f"), "verification_close": format(verification, "f"),
                "verification_source": verification_source,
            } for symbol, business_date, primary, verification, verification_source in close_checks],
        )

        class ResumeMustNotFetchPrimary:
            def __init__(self, timeout_seconds: float = 30.0, attempts: int = 5) -> None:
                pass

            def fetch_bundle(self, symbol: str, start: date, end: date):
                raise AssertionError("successfully checkpointed symbol must not be fetched again")

        with (
            patch("scripts.market_data.historical_bars.CsiIndexSource", FakeCsiSource),
            patch("scripts.market_data.historical_bars.evaluate_universe", return_value=[]),
            patch("scripts.market_data.historical_bars.AkshareEastmoneyHistorySource", ResumeMustNotFetchPrimary),
            patch("scripts.market_data.historical_bars.fetch_primary", side_effect=AssertionError("complete verification checkpoint must be reused")),
        ):
            resumed_manifest, resumed_bars, resumed_facts, resumed_adjustments, resumed_references, resumed_checks = run(
                date(2026, 7, 22), mode="sample", current_universe=current,
                primary_calendar=calendar, secondary_calendar=calendar,
                resume_evidence=resume_evidence,
            )
        self.assertEqual(resumed_manifest["resumed_symbol_count"], 1)
        self.assertEqual(resumed_manifest["acquired_symbol_count"], 0)
        self.assertEqual([row.canonical() for row in resumed_bars], [row.canonical() for row in bars])
        self.assertEqual([row.canonical() for row in resumed_facts], [row.canonical() for row in facts])
        self.assertEqual([row.canonical() for row in resumed_adjustments], [row.canonical() for row in adjustments])
        self.assertEqual([row.canonical() for row in resumed_references], [row.canonical() for row in references])
        self.assertEqual(resumed_checks, close_checks)

        with (
            patch("scripts.market_data.historical_bars.CsiIndexSource", FakeCsiSource),
            patch("scripts.market_data.historical_bars.evaluate_universe", return_value=[]),
            patch("scripts.market_data.historical_bars.AkshareEastmoneyHistorySource", side_effect=AssertionError("finalize must not instantiate a public source")),
            patch("scripts.market_data.historical_bars.fetch_primary", side_effect=AssertionError("finalize must not fetch verification data")),
        ):
            finalized_manifest, *_finalized = run(
                date(2026, 7, 22), mode="sample", current_universe=current,
                primary_calendar=calendar, secondary_calendar=calendar,
                resume_evidence=resume_evidence, acquisition_policy="finalize",
            )
        self.assertEqual(finalized_manifest["acquisition_policy"], "finalize")
        self.assertEqual(finalized_manifest["acquired_symbol_count"], 0)
        completion_gate = next(gate for gate in finalized_manifest["gates"] if gate["name"] == "checkpoint_symbol_completion")
        self.assertTrue(completion_gate["passed"])

        incomplete_verification = HistoricalEvidence(
            manifest=resume_evidence.manifest,
            bars=resume_evidence.bars,
            tradeability=resume_evidence.tradeability,
            adjustments=resume_evidence.adjustments,
            references=resume_evidence.references,
            verification_checks=resume_evidence.verification_checks[:1],
        )
        with (
            patch("scripts.market_data.historical_bars.CsiIndexSource", FakeCsiSource),
            patch("scripts.market_data.historical_bars.evaluate_universe", return_value=[]),
            patch("scripts.market_data.historical_bars.AkshareEastmoneyHistorySource", side_effect=AssertionError("finalize must not instantiate a public source")),
            patch("scripts.market_data.historical_bars.fetch_primary", side_effect=AssertionError("finalize must fail before public verification")),
        ):
            with self.assertRaisesRegex(RuntimeError, "finalize policy forbids public market-data requests"):
                run(
                    date(2026, 7, 22), mode="sample", current_universe=current,
                    primary_calendar=calendar, secondary_calendar=calendar,
                    resume_evidence=incomplete_verification, acquisition_policy="finalize",
                )
        verification_rows_for_refresh = verification_rows()
        refreshed_checkpoints = []

        def capture_refreshed_checkpoint(*values):
            refreshed_checkpoints.append(values)

        with (
            patch("scripts.market_data.historical_bars.CsiIndexSource", FakeCsiSource),
            patch("scripts.market_data.historical_bars.evaluate_universe", return_value=[]),
            patch("scripts.market_data.historical_bars.AkshareEastmoneyHistorySource", ResumeMustNotFetchPrimary),
            patch("scripts.market_data.historical_bars.fetch_primary", return_value=({"600519": verification_rows_for_refresh}, {})),
        ):
            refreshed_manifest, _bars, _facts, _adjustments, _references, refreshed_checks = run(
                date(2026, 7, 22), mode="sample", current_universe=current,
                primary_calendar=calendar, secondary_calendar=calendar,
                resume_evidence=incomplete_verification,
                checkpoint_callback=capture_refreshed_checkpoint,
                acquisition_policy="repair",
            )
        cross_source_gate = next(gate for gate in refreshed_manifest["gates"] if gate["name"] == "historical_cross_source_coverage")
        self.assertTrue(cross_source_gate["passed"])
        self.assertEqual(refreshed_checks, close_checks)
        self.assertEqual(len(refreshed_checkpoints), 1)
        self.assertEqual(refreshed_checkpoints[0][0], "600519")
        self.assertEqual(len(refreshed_checkpoints[0][5]), 3)


if __name__ == "__main__":
    unittest.main()

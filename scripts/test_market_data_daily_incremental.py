from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import DailyBar
from scripts.market_data.daily_adjustments import (
    PreviousAdjustedState,
    build_daily_adjusted_bars,
    evaluate_daily_adjustments,
)
from scripts.market_data.daily_incremental import (
    DailyIncrementalPlan,
    build_incremental_evidence,
    build_incremental_plan,
    fetch_missing_bars,
    latest_closed_session,
    write_outputs,
)
from scripts.market_data.daily_incremental_runner import _reusable_existing_keys
from scripts.market_data.daily_quality_gates import evaluate_daily_incremental
from scripts.market_data.manifest import sha256
from scripts.market_data.quality_gates import accepted
from scripts.market_data.historical_contracts import AdjustmentEvent
from scripts.market_data.tradeability_contracts import TradeabilityFact


SHANGHAI = ZoneInfo("Asia/Shanghai")
TARGET = date(2026, 7, 27)
PREVIOUS = date(2026, 7, 24)


def bar(source: str, symbol: str, close: str = "10.00", *, business_date: date = TARGET) -> DailyBar:
    price = Decimal(close)
    return DailyBar(
        source=source,
        symbol=symbol,
        exchange="SSE" if symbol.startswith("6") else "SZSE",
        business_date=business_date,
        open=price,
        high=price + Decimal("0.10"),
        low=price - Decimal("0.10"),
        close=price,
        previous_close=price,
        volume_shares=10_000,
        amount_cny=price * 10_000,
        turnover_percent=Decimal("0.10"),
        trade_status="trading",
        is_st=False,
    )


def fact(
    symbol: str,
    index_code: str,
    *,
    has_bar: bool = True,
    suspended: bool = False,
    can_buy: bool = True,
    can_sell: bool = True,
    block_reasons: tuple[str, ...] = (),
) -> TradeabilityFact:
    return TradeabilityFact(
        symbol=symbol,
        business_date=TARGET,
        index_code=index_code,
        has_primary_bar=has_bar,
        has_secondary_status=True,
        is_suspended=suspended,
        is_st=False,
        listing_age_sessions=100,
        limit_rate=Decimal("0.10"),
        limit_up=Decimal("11.00"),
        limit_down=Decimal("9.00"),
        at_limit_up=False,
        at_limit_down=False,
        one_price_limit_up=False,
        one_price_limit_down=False,
        can_buy=can_buy,
        can_sell=can_sell,
        block_reasons=block_reasons,
    )


def small_plan() -> DailyIncrementalPlan:
    return DailyIncrementalPlan(
        observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
        target_session=TARGET,
        previous_session=PREVIOUS,
        snapshot_effective_session=PREVIOUS,
        expected_membership=(("000001", "000905"), ("600519", "000300")),
        accepted_existing_symbols=(),
        fetch_symbols=("000001", "600519"),
        verification_symbols=("000001", "600519"),
        primary_calendar_sha256="a" * 64,
        secondary_calendar_sha256="b" * 64,
        universe_sha256="c" * 64,
    )


def healthy_evidence(plan: DailyIncrementalPlan | None = None):
    resolved = plan or small_plan()
    primary = [bar("akshare_eastmoney", "000001"), bar("akshare_eastmoney", "600519")]
    facts = [fact("000001", "000905"), fact("600519", "000300")]
    verification = [bar("baostock", "000001"), bar("baostock", "600519")]
    closes = {"000001": Decimal("10"), "600519": Decimal("10")}
    states, adjusted = adjustment_inputs(resolved, primary, closes)
    return build_incremental_evidence(
        plan=resolved,
        primary_bars=primary,
        tradeability_facts=facts,
        verification_bars=verification,
        adjusted_bars=adjusted,
        adjustment_events=[],
        previous_adjusted_states=states,
        accepted_previous_closes=closes,
        reported_previous_closes=closes,
    )


def adjustment_inputs(
    plan: DailyIncrementalPlan,
    primary: list[DailyBar],
    reported_closes: dict[str, Decimal],
) -> tuple[dict[str, PreviousAdjustedState], list]:
    states = {
        row.symbol: PreviousAdjustedState(
            symbol=row.symbol,
            business_date=plan.previous_session,
            raw_close=Decimal("10"),
            qfq_factor=Decimal("1"),
            hfq_factor=Decimal("1"),
            source_dataset_id="fixture-predecessor",
        )
        for row in primary
    }
    adjusted = build_daily_adjusted_bars(
        target_session=plan.target_session,
        previous_session=plan.previous_session,
        membership=plan.membership,
        primary_bars=primary,
        previous_states=states,
        reported_previous_closes=reported_closes,
    )
    return states, adjusted


class DailyIncrementalTests(unittest.TestCase):
    def calendar(self, sessions=(PREVIOUS, TARGET)) -> TradingCalendar:
        return TradingCalendar.build("fixture", date(2026, 7, 1), TARGET, sessions)

    def full_snapshots(self):
        return {
            PREVIOUS: {
                "000300": tuple(f"6{value:05d}" for value in range(1, 301)),
                "000905": tuple(f"0{value:05d}" for value in range(1, 501)),
            }
        }

    def test_latest_closed_session_uses_shanghai_readiness_cutoff(self) -> None:
        calendar = self.calendar()
        before_ready = datetime(2026, 7, 27, 16, 29, tzinfo=SHANGHAI)
        ready = datetime(2026, 7, 27, 16, 30, tzinfo=SHANGHAI)
        self.assertEqual(latest_closed_session(calendar, before_ready), PREVIOUS)
        self.assertEqual(latest_closed_session(calendar, ready), TARGET)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            latest_closed_session(calendar, datetime(2026, 7, 27, 17, 0))

    def test_stale_or_misaligned_calendar_fails_closed(self) -> None:
        stale = TradingCalendar.build("stale", date(2026, 7, 1), PREVIOUS, [PREVIOUS])
        with self.assertRaisesRegex(ValueError, "stale"):
            latest_closed_session(stale, datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI))
        mismatched = TradingCalendar.build("other", date(2026, 7, 1), TARGET, [date(2026, 7, 23), TARGET])
        with self.assertRaisesRegex(RuntimeError, "not aligned"):
            build_incremental_plan(
                observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
                primary_calendar=self.calendar(),
                secondary_calendar=mismatched,
                snapshots=self.full_snapshots(),
            )

    def test_plan_uses_point_in_time_csi800_and_fetches_only_missing_symbols(self) -> None:
        observed_at = datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI)
        existing = {(f"6{value:05d}", TARGET) for value in range(1, 11)}
        first = build_incremental_plan(
            observed_at=observed_at,
            primary_calendar=self.calendar(),
            secondary_calendar=self.calendar(),
            snapshots=self.full_snapshots(),
            accepted_existing_keys=existing,
        )
        repeated = build_incremental_plan(
            observed_at=observed_at,
            primary_calendar=self.calendar(),
            secondary_calendar=self.calendar(),
            snapshots=self.full_snapshots(),
            accepted_existing_keys={(symbol, TARGET) for symbol, _index in first.expected_membership},
        )
        self.assertEqual(len(first.expected_membership), 800)
        self.assertEqual(len(first.accepted_existing_symbols), 10)
        self.assertEqual(len(first.fetch_symbols), 790)
        self.assertEqual(len(first.verification_symbols), 40)
        self.assertEqual(repeated.fetch_symbols, ())
        self.assertEqual(first.scope_sha256, repeated.scope_sha256)

    def test_corporate_action_candidates_are_not_reused_as_existing_checkpoints(self) -> None:
        keys = _reusable_existing_keys(
            ("000001", "000002", "600000"), TARGET, ("000002", "600000"),
        )
        self.assertEqual(keys, (("000001", TARGET),))

    def test_weekend_retry_keeps_the_same_business_scope_hash(self) -> None:
        friday_calendar = TradingCalendar.build("fixture", date(2026, 7, 1), TARGET, [PREVIOUS, TARGET])
        sunday_calendar = TradingCalendar.build("fixture", date(2026, 7, 1), date(2026, 8, 2), [PREVIOUS, TARGET])
        friday = build_incremental_plan(
            observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
            primary_calendar=friday_calendar, secondary_calendar=friday_calendar,
            snapshots=self.full_snapshots(),
        )
        weekend = build_incremental_plan(
            observed_at=datetime(2026, 8, 2, 12, 0, tzinfo=SHANGHAI),
            primary_calendar=sunday_calendar, secondary_calendar=sunday_calendar,
            snapshots=self.full_snapshots(),
        )
        self.assertEqual(friday.target_session, weekend.target_session)
        self.assertEqual(friday.scope_sha256, weekend.scope_sha256)

    def test_scope_identity_ignores_provenance_but_not_membership(self) -> None:
        baseline = small_plan()
        changed_provenance = replace(
            baseline,
            observed_at=datetime(2026, 7, 29, 17, 0, tzinfo=SHANGHAI),
            snapshot_effective_session=TARGET,
            primary_calendar_sha256="d" * 64,
            secondary_calendar_sha256="e" * 64,
            universe_sha256="f" * 64,
        )
        self.assertEqual(baseline.scope_sha256, changed_provenance.scope_sha256)
        self.assertNotEqual(baseline.canonical(), changed_provenance.canonical())
        changed_membership = replace(
            baseline,
            expected_membership=(("000001", "000300"), ("600519", "000300")),
        )
        self.assertNotEqual(baseline.scope_sha256, changed_membership.scope_sha256)

    def test_corporate_action_inventory_is_scope_bound_and_in_universe_only(self) -> None:
        baseline = small_plan()
        record = {
            "symbol": "000001",
            "ex_dividend_date": TARGET.isoformat(),
            "cash_per_ten_shares": "0.15",
        }
        candidate = replace(
            baseline,
            corporate_action_inventory_count=1,
            corporate_action_inventory_sha256=sha256([record]),
            corporate_action_symbols=("000001",),
        )
        self.assertNotEqual(baseline.scope_sha256, candidate.scope_sha256)
        self.assertEqual(candidate.canonical()["corporate_action_symbols"], ["000001"])
        with self.assertRaisesRegex(ValueError, "inside the expected universe"):
            replace(candidate, corporate_action_symbols=("601866",))

    def test_invalid_universe_size_or_overlap_fails_closed(self) -> None:
        snapshots = self.full_snapshots()
        snapshots[PREVIOUS]["000905"] = snapshots[PREVIOUS]["000905"][:-1]
        with self.assertRaisesRegex(ValueError, "universe sizes"):
            build_incremental_plan(
                observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
                primary_calendar=self.calendar(), secondary_calendar=self.calendar(), snapshots=snapshots,
            )

        overlap = self.full_snapshots()
        overlap[PREVIOUS]["000905"] = ("600001", *overlap[PREVIOUS]["000905"][1:])
        with self.assertRaisesRegex(ValueError, "overlap"):
            build_incremental_plan(
                observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
                primary_calendar=self.calendar(), secondary_calendar=self.calendar(), snapshots=overlap,
            )

        duplicate = self.full_snapshots()
        duplicate[PREVIOUS]["000300"] = (*duplicate[PREVIOUS]["000300"], "600001")
        with self.assertRaisesRegex(ValueError, "rows=301 unique=300"):
            build_incremental_plan(
                observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
                primary_calendar=self.calendar(), secondary_calendar=self.calendar(), snapshots=duplicate,
            )

    def test_injected_fetcher_is_called_only_for_missing_symbols(self) -> None:
        plan = replace(small_plan(), accepted_existing_symbols=("000001",), fetch_symbols=("600519",))
        calls: list[tuple[str, date, date]] = []

        def fetcher(symbol: str, start: date, end: date) -> list[DailyBar]:
            calls.append((symbol, start, end))
            return [bar("fixture", symbol)]

        rows, failures = fetch_missing_bars(plan, fetcher, attempts=2)
        self.assertEqual(calls, [("600519", TARGET, TARGET)])
        self.assertEqual([row.symbol for row in rows], ["600519"])
        self.assertEqual(failures, {})

    def test_fetch_failure_is_bounded_and_explicit(self) -> None:
        calls = 0

        def failing_fetcher(symbol: str, start: date, end: date) -> list[DailyBar]:
            nonlocal calls
            calls += 1
            raise ConnectionError("offline")

        rows, failures = fetch_missing_bars(
            replace(small_plan(), accepted_existing_symbols=("000001",), fetch_symbols=("600519",)),
            failing_fetcher,
            attempts=2,
        )
        self.assertEqual(rows, [])
        self.assertEqual(calls, 2)
        self.assertIn("ConnectionError: offline", failures["600519"])

    def test_fetch_rejects_extra_or_out_of_scope_rows(self) -> None:
        plan = replace(
            small_plan(), accepted_existing_symbols=("000001",), fetch_symbols=("600519",),
        )

        def noisy_fetcher(symbol: str, start: date, end: date) -> list[DailyBar]:
            return [bar("fixture", symbol), bar("fixture", symbol, business_date=PREVIOUS)]

        rows, failures = fetch_missing_bars(plan, noisy_fetcher)
        self.assertEqual(rows, [])
        self.assertIn("exactly one target-session row", failures["600519"])

    def test_healthy_daily_evidence_passes_but_remains_non_authoritative(self) -> None:
        manifest, primary, facts, verification, adjusted, events = healthy_evidence()
        self.assertTrue(manifest["accepted"], manifest["gates"])
        self.assertFalse(manifest["authoritative"])
        self.assertFalse(manifest["simulation_orders_allowed"])
        self.assertEqual(len(primary), 2)
        self.assertEqual(len(facts), 2)
        self.assertEqual(len(verification), 2)
        self.assertEqual(len(adjusted), 2)
        self.assertEqual(events, [])

    def test_full_csi800_fixture_passes_with_preregistered_verification_sample(self) -> None:
        plan = build_incremental_plan(
            observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
            primary_calendar=self.calendar(),
            secondary_calendar=self.calendar(),
            snapshots=self.full_snapshots(),
        )
        primary = [bar("akshare_eastmoney", symbol) for symbol, _index_code in plan.expected_membership]
        facts = [fact(symbol, index_code) for symbol, index_code in plan.expected_membership]
        verification = [bar("baostock", symbol) for symbol in plan.verification_symbols]
        closes = {symbol: Decimal("10") for symbol, _index_code in plan.expected_membership}
        states, adjusted = adjustment_inputs(plan, primary, closes)
        manifest = build_incremental_evidence(
            plan=plan,
            primary_bars=primary,
            tradeability_facts=facts,
            verification_bars=verification,
            adjusted_bars=adjusted,
            adjustment_events=[],
            previous_adjusted_states=states,
            accepted_previous_closes=closes,
            reported_previous_closes=closes,
        )[0]
        self.assertTrue(manifest["accepted"], manifest["gates"])
        self.assertEqual(manifest["expected_symbol_count"], 800)
        self.assertEqual(manifest["primary_row_count"], 800)
        self.assertEqual(manifest["verification_row_count"], 40)

    def test_verified_corporate_action_reconciles_previous_close_break(self) -> None:
        plan = small_plan()
        primary = [bar("akshare_eastmoney", "000001"), bar("akshare_eastmoney", "600519")]
        verification = [bar("baostock", "000001"), bar("baostock", "600519")]
        states = {
            symbol: PreviousAdjustedState(
                symbol=symbol, business_date=PREVIOUS, raw_close=Decimal("10"),
                qfq_factor=Decimal("1"), hfq_factor=Decimal("1"),
                source_dataset_id="fixture-predecessor",
            )
            for symbol in ("000001", "600519")
        }
        event = AdjustmentEvent(
            "000001", TARGET, Decimal("1"), Decimal("1.052632"),
            source="akshare_sina_factor_multiplicative",
        )
        reported = {"000001": Decimal("9.5"), "600519": Decimal("10")}
        adjusted = build_daily_adjusted_bars(
            target_session=TARGET, previous_session=PREVIOUS, membership=plan.membership,
            primary_bars=primary, previous_states=states,
            reported_previous_closes=reported, adjustment_events=[event],
        )
        facts = [
            replace(fact("000001", "000905"), limit_up=Decimal("10.45"), limit_down=Decimal("8.55")),
            fact("600519", "000300"),
        ]
        manifest = build_incremental_evidence(
            plan=plan, primary_bars=primary, tradeability_facts=facts,
            verification_bars=verification, adjusted_bars=adjusted,
            adjustment_events=[event], previous_adjusted_states=states,
            accepted_previous_closes={"000001": Decimal("10"), "600519": Decimal("10")},
            reported_previous_closes=reported,
        )[0]
        continuity = next(
            gate for gate in manifest["gates"] if gate["name"] == "daily_previous_close_continuity"
        )
        self.assertFalse(continuity["passed"])
        self.assertFalse(continuity["critical"])
        self.assertTrue(manifest["accepted"], manifest["gates"])

    def test_cash_dividend_half_tick_uses_exact_factor_reference(self) -> None:
        symbol = "601866"
        primary = [bar("akshare_eastmoney", symbol, "2.50")]
        state = PreviousAdjustedState(
            symbol=symbol,
            business_date=PREVIOUS,
            raw_close=Decimal("2.4700"),
            qfq_factor=Decimal("1.000000"),
            hfq_factor=Decimal("1.227273"),
            source_dataset_id="accepted-2026-07-30",
        )
        event = AdjustmentEvent(
            symbol,
            TARGET,
            Decimal("1.000000"),
            Decimal("1.236110"),
            source="akshare_sina_factor",
        )
        arguments = {
            "target_session": TARGET,
            "previous_session": PREVIOUS,
            "membership": {symbol: "000905"},
            "primary_bars": primary,
            "previous_states": {symbol: state},
            "reported_previous_closes": {symbol: Decimal("2.4600")},
            "adjustment_events": [event],
        }
        with self.assertRaisesRegex(ValueError, "adjustment factor does not reconcile"):
            build_daily_adjusted_bars(**arguments)

        adjusted = build_daily_adjusted_bars(
            **arguments,
            factor_reference_closes={symbol: Decimal("2.455000")},
        )
        self.assertEqual(adjusted[0].previous_close, Decimal("2.4600"))
        gates = evaluate_daily_adjustments(
            target_session=TARGET,
            previous_session=PREVIOUS,
            membership={symbol: "000905"},
            primary_bars=primary,
            adjusted_bars=adjusted,
            previous_states={symbol: state},
            reported_previous_closes={symbol: Decimal("2.4600")},
            adjustment_events=[event],
            factor_reference_closes={symbol: Decimal("2.455000")},
        )
        lineage_gate = next(gate for gate in gates if gate.name == "daily_adjustment_lineage")
        self.assertTrue(lineage_gate.passed, lineage_gate.details)

        evidence_plan = DailyIncrementalPlan(
            observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
            target_session=TARGET,
            previous_session=PREVIOUS,
            snapshot_effective_session=PREVIOUS,
            expected_membership=((symbol, "000905"),),
            accepted_existing_symbols=(),
            fetch_symbols=(symbol,),
            verification_symbols=(symbol,),
            primary_calendar_sha256="a" * 64,
            secondary_calendar_sha256="b" * 64,
            universe_sha256="c" * 64,
            corporate_action_inventory_count=1,
            corporate_action_inventory_sha256="d" * 64,
            corporate_action_symbols=(symbol,),
        )
        manifest = build_incremental_evidence(
            plan=evidence_plan,
            primary_bars=primary,
            tradeability_facts=[fact(symbol, "000905")],
            verification_bars=[bar("baostock", symbol, "2.50")],
            adjusted_bars=adjusted,
            adjustment_events=[event],
            previous_adjusted_states={symbol: state},
            accepted_previous_closes={symbol: Decimal("2.4700")},
            reported_previous_closes={symbol: Decimal("2.4600")},
            factor_reference_closes={symbol: Decimal("2.455000")},
            lineage_evidence=[{
                "symbol": symbol,
                "target_session": TARGET.isoformat(),
                "kind": "cash_dividend_reference",
                "source": "tencent_archive",
                "details": {"factor_reference_close": "2.455000"},
            }],
        )[0]
        manifest_lineage = next(
            gate for gate in manifest["gates"] if gate["name"] == "daily_adjustment_lineage"
        )
        self.assertTrue(manifest_lineage["passed"], manifest_lineage["details"])
        candidate_gate = next(
            gate for gate in manifest["gates"]
            if gate["name"] == "daily_corporate_action_candidate_evidence"
        )
        self.assertTrue(candidate_gate["passed"], candidate_gate["details"])

        missing_lineage = build_incremental_evidence(
            plan=evidence_plan,
            primary_bars=primary,
            tradeability_facts=[fact(symbol, "000905")],
            verification_bars=[bar("baostock", symbol, "2.50")],
            adjusted_bars=adjusted,
            adjustment_events=[event],
            previous_adjusted_states={symbol: state},
            accepted_previous_closes={symbol: Decimal("2.4700")},
            reported_previous_closes={symbol: Decimal("2.4600")},
            factor_reference_closes={symbol: Decimal("2.455000")},
        )[0]
        self.assertFalse(missing_lineage["accepted"])

        with self.assertRaisesRegex(ValueError, "does not round to reported previous close"):
            build_daily_adjusted_bars(
                **arguments,
                factor_reference_closes={symbol: Decimal("2.440000")},
            )

    def test_exact_cash_evidence_exposes_incomparable_vendor_factor_as_diagnostic(self) -> None:
        symbol = "601727"
        primary = [bar("akshare_eastmoney", symbol, "10.10")]
        state = PreviousAdjustedState(
            symbol=symbol,
            business_date=PREVIOUS,
            raw_close=Decimal("10.020730"),
            qfq_factor=Decimal("1.000000"),
            hfq_factor=Decimal("1.000000"),
            source_dataset_id="accepted-predecessor-with-another-factor-base",
        )
        event = AdjustmentEvent(
            symbol,
            TARGET,
            Decimal("1.000000"),
            Decimal("1.030020"),
            source="akshare_sina_absolute_factor",
        )
        arguments = {
            "target_session": TARGET,
            "previous_session": PREVIOUS,
            "membership": {symbol: "000905"},
            "primary_bars": primary,
            "previous_states": {symbol: state},
            "reported_previous_closes": {symbol: Decimal("10.0000")},
            "adjustment_events": [event],
        }
        with self.assertRaisesRegex(ValueError, "adjustment factor does not reconcile"):
            build_daily_adjusted_bars(**arguments)

        adjusted = build_daily_adjusted_bars(
            **arguments,
            factor_reference_closes={symbol: Decimal("10.000000")},
        )
        self.assertEqual(adjusted[0].qfq_factor, state.qfq_factor)
        self.assertEqual(adjusted[0].hfq_factor, state.hfq_factor)
        self.assertEqual(adjusted[0].factor_source, "rqalpha_deferred_cash_action")
        gates = evaluate_daily_adjustments(
            target_session=TARGET,
            previous_session=PREVIOUS,
            membership={symbol: "000905"},
            primary_bars=primary,
            adjusted_bars=adjusted,
            previous_states={symbol: state},
            reported_previous_closes={symbol: Decimal("10.0000")},
            adjustment_events=[event],
            factor_reference_closes={symbol: Decimal("10.000000")},
        )
        lineage = next(gate for gate in gates if gate.name == "daily_adjustment_lineage")
        comparability = next(
            gate for gate in gates if gate.name == "daily_vendor_absolute_factor_comparability"
        )
        self.assertTrue(lineage.passed, lineage.details)
        self.assertFalse(comparability.passed)
        self.assertFalse(comparability.critical)
        self.assertEqual(comparability.details, (
            "601727:expected=1.002073:observed=1.030020",
        ))
        plan = DailyIncrementalPlan(
            observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
            target_session=TARGET,
            previous_session=PREVIOUS,
            snapshot_effective_session=PREVIOUS,
            expected_membership=((symbol, "000905"),),
            accepted_existing_symbols=(),
            fetch_symbols=(symbol,),
            verification_symbols=(symbol,),
            primary_calendar_sha256="a" * 64,
            secondary_calendar_sha256="b" * 64,
            universe_sha256="c" * 64,
            corporate_action_inventory_count=1,
            corporate_action_inventory_sha256="d" * 64,
            corporate_action_symbols=(symbol,),
        )
        manifest = build_incremental_evidence(
            plan=plan,
            primary_bars=primary,
            tradeability_facts=[fact(symbol, "000905")],
            verification_bars=[bar("baostock", symbol, "10.10")],
            adjusted_bars=adjusted,
            adjustment_events=[event],
            previous_adjusted_states={symbol: state},
            accepted_previous_closes={symbol: state.raw_close},
            reported_previous_closes={symbol: Decimal("10.0000")},
            factor_reference_closes={symbol: Decimal("10.000000")},
            lineage_evidence=[{
                "symbol": symbol,
                "target_session": TARGET.isoformat(),
                "kind": "cash_dividend_reference",
                "source": "tencent_archive",
                "details": {"factor_reference_close": "10.000000"},
            }],
        )[0]
        self.assertTrue(manifest["accepted"], manifest["gates"])
        manifest_comparability = next(
            gate for gate in manifest["gates"]
            if gate["name"] == "daily_vendor_absolute_factor_comparability"
        )
        self.assertFalse(manifest_comparability["passed"])
        self.assertFalse(manifest_comparability["critical"])

    def test_missing_active_bar_fails_but_confirmed_suspension_is_allowed(self) -> None:
        plan = small_plan()
        primary = [bar("akshare_eastmoney", "600519")]
        active_facts = [fact("000001", "000905", has_bar=False, can_buy=False, can_sell=False, block_reasons=("missing_primary_bar",)), fact("600519", "000300")]
        verification = [bar("baostock", "600519")]
        closes = {"600519": Decimal("10")}
        states, adjusted = adjustment_inputs(plan, primary, closes)
        failed = build_incremental_evidence(
            plan=plan, primary_bars=primary, tradeability_facts=active_facts,
            verification_bars=verification, adjusted_bars=adjusted, adjustment_events=[],
            previous_adjusted_states=states,
            accepted_previous_closes=closes, reported_previous_closes=closes,
        )[0]
        self.assertFalse(failed["accepted"])

        suspended_facts = [
            fact("000001", "000905", has_bar=False, suspended=True, can_buy=False, can_sell=False, block_reasons=("missing_primary_bar", "suspended")),
            fact("600519", "000300"),
        ]
        passed = build_incremental_evidence(
            plan=plan, primary_bars=primary, tradeability_facts=suspended_facts,
            verification_bars=verification, adjusted_bars=adjusted, adjustment_events=[],
            previous_adjusted_states=states,
            accepted_previous_closes=closes, reported_previous_closes=closes,
            primary_failures={"000001": "RuntimeError: no trading bar"},
        )[0]
        self.assertTrue(passed["accepted"], passed["gates"])
        fetch_gate = next(gate for gate in passed["gates"] if gate["name"] == "daily_primary_fetch_failures")
        self.assertFalse(fetch_gate["passed"])
        self.assertFalse(fetch_gate["critical"])

    def test_unsafe_tradeability_previous_close_or_cross_source_conflict_fails(self) -> None:
        plan = small_plan()
        primary = [bar("akshare_eastmoney", "000001"), bar("akshare_eastmoney", "600519")]
        facts = [
            fact("000001", "000905", suspended=True, can_buy=True, block_reasons=("suspended",)),
            fact("600519", "000300"),
        ]
        verification = [bar("baostock", "000001", "11.00"), bar("baostock", "600519", "11.00")]
        gates = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=plan.membership,
            primary_bars=primary, tradeability_facts=facts, verification_bars=verification,
            verification_symbols=plan.verification_symbols,
            accepted_previous_closes={"000001": Decimal("10"), "600519": Decimal("10")},
            reported_previous_closes={"000001": Decimal("9"), "600519": Decimal("9")},
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertFalse(by_name["daily_tradeability_fail_closed"].passed)
        self.assertFalse(by_name["daily_previous_close_continuity"].passed)
        self.assertFalse(by_name["daily_cross_source_close"].passed)
        self.assertFalse(accepted(gates))

    def test_price_limit_values_are_recomputed_from_reported_previous_close(self) -> None:
        plan = small_plan()
        primary = [bar("akshare_eastmoney", "000001"), bar("akshare_eastmoney", "600519")]
        facts = [replace(fact("000001", "000905"), limit_up=Decimal("12.00")), fact("600519", "000300")]
        verification = [bar("baostock", "000001"), bar("baostock", "600519")]
        closes = {"000001": Decimal("10"), "600519": Decimal("10")}
        gates = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=plan.membership,
            primary_bars=primary, tradeability_facts=facts, verification_bars=verification,
            verification_symbols=plan.verification_symbols,
            accepted_previous_closes=closes, reported_previous_closes=closes,
        )
        self.assertFalse(next(gate for gate in gates if gate.name == "daily_price_limit_reconciliation").passed)
        self.assertFalse(accepted(gates))

        new_listing = replace(
            fact("000001", "000905"),
            listing_age_sessions=4,
            limit_rate=Decimal("0.10"),
            limit_up=Decimal("11.00"),
            limit_down=Decimal("9.00"),
        )
        new_listing_gates = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=plan.membership,
            primary_bars=primary, tradeability_facts=[new_listing, fact("600519", "000300")],
            verification_bars=verification, verification_symbols=plan.verification_symbols,
            accepted_previous_closes=closes, reported_previous_closes=closes,
        )
        self.assertFalse(next(
            gate for gate in new_listing_gates if gate.name == "daily_price_limit_reconciliation"
        ).passed)

    def test_tradeability_state_cannot_be_hidden_by_missing_reason_text(self) -> None:
        plan = small_plan()
        primary = [bar("akshare_eastmoney", "000001"), bar("akshare_eastmoney", "600519")]
        unsafe = replace(
            fact("000001", "000905"),
            has_secondary_status=False,
            can_buy=True,
            can_sell=True,
            block_reasons=(),
        )
        facts = [unsafe, fact("600519", "000300")]
        verification = [bar("baostock", "000001"), bar("baostock", "600519")]
        closes = {"000001": Decimal("10"), "600519": Decimal("10")}
        gates = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=plan.membership,
            primary_bars=primary, tradeability_facts=facts, verification_bars=verification,
            verification_symbols=plan.verification_symbols,
            accepted_previous_closes=closes, reported_previous_closes=closes,
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertFalse(by_name["daily_tradeability_fail_closed"].passed)
        self.assertFalse(by_name["daily_tradeability_reason_completeness"].passed)

    def test_only_independently_confirmed_suspension_is_excluded_from_coverage(self) -> None:
        plan = small_plan()
        facts = [
            replace(
                fact("000001", "000905", has_bar=False, suspended=True, can_buy=False, can_sell=False),
                has_secondary_status=False,
                block_reasons=("missing_primary_bar", "missing_secondary_status", "suspended"),
            ),
            fact("600519", "000300"),
        ]
        closes = {"600519": Decimal("10")}
        gates = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=plan.membership,
            primary_bars=[bar("akshare_eastmoney", "600519")], tradeability_facts=facts,
            verification_bars=[bar("baostock", "600519")], verification_symbols=plan.verification_symbols,
            accepted_previous_closes=closes, reported_previous_closes=closes,
        )
        coverage = next(gate for gate in gates if gate.name == "daily_active_bar_coverage")
        self.assertFalse(coverage.passed)
        self.assertIn("000001", coverage.details)

    def test_unknown_st_status_blocks_symbol_without_guessing_price_limit(self) -> None:
        plan = small_plan()
        unknown = replace(
            fact("000001", "000905"),
            is_st=None,
            limit_rate=None,
            limit_up=None,
            limit_down=None,
            can_buy=False,
            can_sell=False,
            block_reasons=("unknown_st_status",),
        )
        closes = {"000001": Decimal("10"), "600519": Decimal("10")}
        gates = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=plan.membership,
            primary_bars=[bar("akshare_eastmoney", "000001"), bar("akshare_eastmoney", "600519")],
            tradeability_facts=[unknown, fact("600519", "000300")],
            verification_bars=[bar("baostock", "000001"), bar("baostock", "600519")],
            verification_symbols=plan.verification_symbols,
            accepted_previous_closes=closes, reported_previous_closes=closes,
        )
        self.assertTrue(next(gate for gate in gates if gate.name == "daily_price_limit_reconciliation").passed)
        self.assertTrue(accepted(gates), [gate.canonical() for gate in gates])

    def test_cross_source_must_be_independent(self) -> None:
        manifest, _primary, _facts, _verification, _adjusted, _events = healthy_evidence()
        self.assertTrue(manifest["accepted"])
        plan = small_plan()
        rows = [bar("akshare_eastmoney", "000001"), bar("akshare_eastmoney", "600519")]
        facts = [fact("000001", "000905"), fact("600519", "000300")]
        closes = {"000001": Decimal("10"), "600519": Decimal("10")}
        states, adjusted = adjustment_inputs(plan, rows, closes)
        same_source = build_incremental_evidence(
            plan=plan, primary_bars=rows, tradeability_facts=facts, verification_bars=rows,
            adjusted_bars=adjusted, adjustment_events=[], previous_adjusted_states=states,
            accepted_previous_closes=closes, reported_previous_closes=closes,
        )[0]
        self.assertFalse(same_source["accepted"])

    def test_cross_source_volume_and_amount_units_must_match(self) -> None:
        plan = small_plan()
        primary = [bar("akshare_eastmoney", "000001"), bar("akshare_eastmoney", "600519")]
        verification = [
            replace(bar("baostock", "000001"), volume_shares=100_000, amount_cny=Decimal("1000000")),
            bar("baostock", "600519"),
        ]
        closes = {"000001": Decimal("10"), "600519": Decimal("10")}
        gates = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=plan.membership,
            primary_bars=primary,
            tradeability_facts=[fact("000001", "000905"), fact("600519", "000300")],
            verification_bars=verification, verification_symbols=plan.verification_symbols,
            accepted_previous_closes=closes, reported_previous_closes=closes,
        )
        self.assertFalse(next(gate for gate in gates if gate.name == "daily_cross_source_volume_units").passed)
        self.assertFalse(next(gate for gate in gates if gate.name == "daily_cross_source_amount").passed)
        self.assertFalse(accepted(gates))

    def test_exact_98_percent_active_coverage_boundary(self) -> None:
        expected = {f"{value:06d}": "000905" for value in range(100)}
        primary_98 = [bar("primary", symbol) for symbol in list(expected)[:98]]
        facts = [
            fact(symbol, "000905", has_bar=symbol in {row.symbol for row in primary_98}, can_buy=symbol in {row.symbol for row in primary_98}, can_sell=symbol in {row.symbol for row in primary_98}, block_reasons=() if symbol in {row.symbol for row in primary_98} else ("missing_primary_bar",))
            for symbol in expected
        ]
        verification_symbols = (list(expected)[0],)
        verification = [bar("secondary", verification_symbols[0])]
        previous = {symbol: Decimal("10") for symbol in expected if symbol in {row.symbol for row in primary_98}}
        gates_98 = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=expected,
            primary_bars=primary_98, tradeability_facts=facts, verification_bars=verification,
            verification_symbols=verification_symbols,
            accepted_previous_closes=previous, reported_previous_closes=previous,
        )
        self.assertTrue(next(gate for gate in gates_98 if gate.name == "daily_active_bar_coverage").passed)
        self.assertTrue(accepted(gates_98), [gate.canonical() for gate in gates_98])
        gates_97 = evaluate_daily_incremental(
            target_session=TARGET, previous_session=PREVIOUS, expected_membership=expected,
            primary_bars=primary_98[:97], tradeability_facts=[
                replace(item, has_primary_bar=item.symbol in {row.symbol for row in primary_98[:97]}, can_buy=False, can_sell=False, block_reasons=("missing_primary_bar",))
                if item.symbol not in {row.symbol for row in primary_98[:97]} else item
                for item in facts
            ],
            verification_bars=verification, verification_symbols=verification_symbols,
            accepted_previous_closes=previous, reported_previous_closes=previous,
        )
        self.assertFalse(next(gate for gate in gates_97 if gate.name == "daily_active_bar_coverage").passed)

    def test_manifest_and_compressed_outputs_are_deterministic(self) -> None:
        first = healthy_evidence()
        second = build_incremental_evidence(
            plan=small_plan(),
            primary_bars=reversed(first[1]),
            tradeability_facts=reversed(first[2]),
            verification_bars=reversed(first[3]),
            adjusted_bars=reversed(first[4]),
            adjustment_events=reversed(first[5]),
            previous_adjusted_states={
                symbol: PreviousAdjustedState(
                    symbol=symbol, business_date=PREVIOUS, raw_close=Decimal("10"),
                    qfq_factor=Decimal("1"), hfq_factor=Decimal("1"),
                    source_dataset_id="fixture-predecessor",
                )
                for symbol in ("000001", "600519")
            },
            accepted_previous_closes={"000001": Decimal("10"), "600519": Decimal("10")},
            reported_previous_closes={"000001": Decimal("10"), "600519": Decimal("10")},
        )
        self.assertEqual(first[0]["primary_sha256"], second[0]["primary_sha256"])
        self.assertEqual(first[0]["quality_sha256"], second[0]["quality_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            left = Path(temporary) / "left"
            right = Path(temporary) / "right"
            write_outputs(left, first[0], first[1], first[2], first[3], first[4], first[5])
            write_outputs(right, second[0], second[1], second[2], second[3], second[4], second[5])
            self.assertEqual((left / "daily-primary-bars.json.gz").read_bytes(), (right / "daily-primary-bars.json.gz").read_bytes())
            self.assertEqual((left / "daily-tradeability.json.gz").read_bytes(), (right / "daily-tradeability.json.gz").read_bytes())
            self.assertEqual((left / "manifest.json").read_bytes(), (right / "manifest.json").read_bytes())

            invalid_manifest = dict(first[0])
            invalid_manifest["primary_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "manifest does not match"):
                write_outputs(
                    Path(temporary) / "invalid",
                    invalid_manifest,
                    first[1],
                    first[2],
                    first[3],
                    first[4],
                    first[5],
                )
            self.assertFalse((Path(temporary) / "invalid").exists())

    def test_returned_market_rows_use_the_manifest_hash_order(self) -> None:
        baseline = healthy_evidence()
        primary_source_by_symbol = {"000001": "z_primary", "600519": "a_primary"}
        verification_source_by_symbol = {"000001": "z_verification", "600519": "a_verification"}
        primary = [
            replace(row, source=primary_source_by_symbol[row.symbol])
            for row in baseline[1]
        ]
        verification = [
            replace(row, source=verification_source_by_symbol[row.symbol])
            for row in baseline[3]
        ]
        adjusted = [
            replace(row, primary_source=primary_source_by_symbol[row.symbol])
            for row in baseline[4]
        ]
        result = build_incremental_evidence(
            plan=small_plan(), primary_bars=primary, tradeability_facts=baseline[2],
            verification_bars=verification, adjusted_bars=adjusted,
            adjustment_events=baseline[5],
            previous_adjusted_states={
                symbol: PreviousAdjustedState(
                    symbol=symbol, business_date=PREVIOUS, raw_close=Decimal("10"),
                    qfq_factor=Decimal("1"), hfq_factor=Decimal("1"),
                    source_dataset_id="fixture-predecessor",
                )
                for symbol in ("000001", "600519")
            },
            accepted_previous_closes={"000001": Decimal("10"), "600519": Decimal("10")},
            reported_previous_closes={"000001": Decimal("10"), "600519": Decimal("10")},
        )
        manifest, returned_primary, _facts, returned_verification, _adjusted, _events = result
        self.assertEqual([row.source for row in returned_primary], ["a_primary", "z_primary"])
        self.assertEqual(
            manifest["primary_sha256"],
            sha256([row.canonical() for row in returned_primary]),
        )
        self.assertEqual(
            manifest["verification_sha256"],
            sha256([row.canonical() for row in returned_verification]),
        )


if __name__ == "__main__":
    unittest.main()

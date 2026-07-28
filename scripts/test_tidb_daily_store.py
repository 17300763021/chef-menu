from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from scripts.market_data.contracts import DailyBar
from scripts.market_data.daily_adjustments import PreviousAdjustedState, build_daily_adjusted_bars
from scripts.market_data.daily_incremental import DailyIncrementalPlan
from scripts.market_data.daily_incremental_runner import _select_target, capture_symbol
from scripts.market_data.historical_contracts import AdjustmentEvent
from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_daily_store import (
    DailyEvidence,
    default_daily_dataset_id,
    ensure_daily_schema,
    latest_accepted_lineage,
    load_daily_checkpoint_evidence,
    load_previous_adjusted_states,
    publish_daily_run,
    publish_daily_symbol_checkpoint,
)
from scripts.market_data.tradeability import derive_tradeability


TARGET = date(2026, 7, 27)
PREVIOUS = date(2026, 7, 24)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.last_sql = ""
        self.last_params: Any = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.last_sql = sql
        self.last_params = params
        self.connection.executed.append((sql, params))

    def executemany(self, sql: str, rows) -> None:
        values = list(rows)
        self.last_sql = sql
        self.last_params = values
        self.connection.executed_many.append((sql, values))

    def fetchall(self):
        return self.connection.router(self.last_sql, self.last_params)


class FakeConnection:
    def __init__(self, router: Callable[[str, Any], list[tuple[Any, ...]]] | None = None) -> None:
        self.router = router or (lambda sql, params: [])
        self.executed: list[tuple[str, Any]] = []
        self.executed_many: list[tuple[str, list[Any]]] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


def raw_bar(symbol: str = "000001", source: str = "akshare_eastmoney") -> DailyBar:
    return DailyBar(
        source=source, symbol=symbol, exchange="SZSE", business_date=TARGET,
        open=Decimal("10.00"), high=Decimal("10.10"), low=Decimal("9.90"),
        close=Decimal("10.00"), previous_close=None, volume_shares=10_000,
        amount_cny=Decimal("100000.00"), turnover_percent=Decimal("0.100000"),
        trade_status="trading", is_st=None,
    )


def plan() -> DailyIncrementalPlan:
    return DailyIncrementalPlan(
        observed_at=datetime(2026, 7, 27, 17, 0, tzinfo=SHANGHAI),
        target_session=TARGET, previous_session=PREVIOUS,
        snapshot_effective_session=PREVIOUS,
        expected_membership=(("000001", "000300"),), accepted_existing_symbols=(),
        fetch_symbols=("000001",), verification_symbols=("000001",),
        primary_calendar_sha256="a" * 64, secondary_calendar_sha256="b" * 64,
        universe_sha256="c" * 64,
    )


def previous_state() -> PreviousAdjustedState:
    return PreviousAdjustedState(
        symbol="000001", business_date=PREVIOUS, raw_close=Decimal("10"),
        qfq_factor=Decimal("1"), hfq_factor=Decimal("1"),
        source_dataset_id="base-history",
    )


def complete_evidence() -> DailyEvidence:
    primary = raw_bar()
    state = previous_state()
    adjusted = build_daily_adjusted_bars(
        target_session=TARGET, previous_session=PREVIOUS,
        membership={"000001": "000300"}, primary_bars=[primary],
        previous_states={"000001": state},
        reported_previous_closes={"000001": Decimal("10")},
    )[0]
    fact = derive_tradeability(
        symbol="000001", business_date=TARGET, index_code="000300",
        listing_age_sessions=100,
        primary={"high": primary.high, "low": primary.low, "close": primary.close},
        secondary={"tradestatus": "1", "isST": "0", "preclose": "10"},
    )
    return DailyEvidence(
        manifest={"authoritative": False, "simulation_orders_allowed": False},
        primary_bars=[primary.canonical()], tradeability=[fact.canonical()],
        verification_bars=[raw_bar(source="baostock").canonical()],
        adjusted_bars=[adjusted.canonical()], adjustments=[],
    )


class TiDBDailyStoreTests(unittest.TestCase):
    def test_dataset_id_is_stable_and_scope_bound(self) -> None:
        self.assertEqual(
            default_daily_dataset_id(TARGET, "a" * 64),
            f"m2-daily-{TARGET.isoformat()}-{'a' * 64}",
        )
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            default_daily_dataset_id(TARGET, "short")

    def test_schema_creation_is_idempotent_statement_set(self) -> None:
        connection = FakeConnection()
        ensure_daily_schema(connection)
        sql = "\n".join(statement for statement, _params in connection.executed)
        for table in (
            "m2_daily_runs", "m2_daily_symbol_checkpoints", "m2_daily_primary_bars",
            "m2_daily_adjusted_bars", "m2_daily_tradeability_facts",
            "m2_daily_verification_bars", "m2_daily_adjustment_events",
        ):
            self.assertIn(table, sql)
        self.assertEqual(connection.commits, 1)

    def test_symbol_checkpoint_is_atomic_scope_and_rejects_accepted_mutation(self) -> None:
        evidence = complete_evidence()
        connection = FakeConnection()
        counts = publish_daily_symbol_checkpoint(
            connection, evidence, dataset_id="daily-scope", symbol="000001",
            target_session=TARGET, verification_required=True,
            reported_previous_close=Decimal("10"), status="succeeded",
        )
        self.assertEqual(counts, {
            "primary_bars": 1, "adjusted_bars": 1, "tradeability": 1,
            "verification_bars": 1, "adjustments": 0, "symbol_checkpoints": 1,
        })
        deletes = [sql for sql, _params in connection.executed if sql.strip().startswith("DELETE")]
        self.assertEqual(len(deletes), 5)
        self.assertEqual(connection.commits, 1)

        immutable = FakeConnection(
            lambda sql, params: [(1,)] if "SELECT accepted FROM m2_daily_runs" in sql else [],
        )
        with self.assertRaisesRegex(RuntimeError, "immutable"):
            publish_daily_symbol_checkpoint(
                immutable, evidence, dataset_id="daily-scope", symbol="000001",
                target_session=TARGET, verification_required=True,
                reported_previous_close=Decimal("10"), status="succeeded",
            )

    def test_accepted_aggregate_is_one_time_and_idempotent(self) -> None:
        evidence = complete_evidence()
        scope_hash = "d" * 64
        dataset_id = default_daily_dataset_id(TARGET, scope_hash)
        values = {
            "primary_row_count": len(evidence.primary_bars),
            "adjusted_row_count": len(evidence.adjusted_bars),
            "tradeability_row_count": len(evidence.tradeability),
            "verification_row_count": len(evidence.verification_bars),
            "adjustment_event_count": len(evidence.adjustments),
            "primary_sha256": sha256(evidence.primary_bars),
            "adjusted_sha256": sha256(evidence.adjusted_bars),
            "tradeability_sha256": sha256(evidence.tradeability),
            "verification_sha256": sha256(evidence.verification_bars),
            "adjustments_sha256": sha256(evidence.adjustments),
        }
        manifest = {
            **evidence.manifest, **values,
            "accepted": True, "manifest_version": "m2-daily-incremental-manifest-v1",
            "target_session": TARGET.isoformat(), "previous_session": PREVIOUS.isoformat(),
            "snapshot_effective_session": PREVIOUS.isoformat(), "scope_sha256": scope_hash,
            "expected_symbol_count": 1, "quality_sha256": "e" * 64,
        }
        publication = DailyEvidence(
            manifest=manifest, primary_bars=evidence.primary_bars,
            tradeability=evidence.tradeability, verification_bars=evidence.verification_bars,
            adjusted_bars=evidence.adjusted_bars, adjustments=evidence.adjustments,
        )

        def fresh_router(sql: str, params: Any):
            if "SELECT symbol, status" in sql:
                return [("000001", "succeeded")]
            return []

        connection = FakeConnection(fresh_router)
        result = publish_daily_run(
            connection, publication, dataset_id=dataset_id,
            base_history_dataset_id="base", predecessor_dataset_id="base",
        )
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(connection.commits, 1)

        manifest_hash = sha256(manifest)

        def replay_router(sql: str, params: Any):
            if "SELECT symbol, status" in sql:
                return [("000001", "succeeded")]
            if "WHERE target_session" in sql:
                return [(dataset_id, manifest_hash)]
            return []

        replay = publish_daily_run(
            FakeConnection(replay_router), publication, dataset_id=dataset_id,
            base_history_dataset_id="base", predecessor_dataset_id="base",
        )
        self.assertTrue(replay["idempotent_replay"])

    def test_predecessor_loading_distinguishes_daily_and_history_lineage(self) -> None:
        def daily_router(sql: str, params: Any):
            if "SELECT target_session, accepted" in sql:
                return [(PREVIOUS, 1, 0, 0)]
            if "FROM m2_daily_adjusted_bars" in sql:
                return [("000001", PREVIOUS, Decimal("10"), Decimal("1"), Decimal("2"))]
            return []

        daily = load_previous_adjusted_states(
            FakeConnection(daily_router), predecessor_dataset_id="daily-prev",
            previous_session=PREVIOUS,
        )
        self.assertEqual(daily["000001"].hfq_factor, Decimal("2"))

        def history_router(sql: str, params: Any):
            if "SELECT target_session, accepted" in sql:
                return []
            if "FROM m2_history_runs" in sql:
                return [(PREVIOUS, 1, 0, 0)]
            if "JOIN m2_historical_bars" in sql:
                return [("000001", PREVIOUS, Decimal("10"), Decimal("1"), Decimal("2"))]
            return []

        history = load_previous_adjusted_states(
            FakeConnection(history_router), predecessor_dataset_id="history-base",
            previous_session=PREVIOUS,
        )
        self.assertEqual(history["000001"].source_dataset_id, "history-base")

    def test_checkpoint_readback_reconciles_row_and_symbol_hashes(self) -> None:
        evidence = complete_evidence()
        primary = evidence.primary_bars[0]
        adjusted = evidence.adjusted_bars[0]
        fact = evidence.tradeability[0]
        verification = evidence.verification_bars[0]

        def router_with_primary_hash(primary_hash: str):
            def router(sql: str, params: Any):
                if "FROM m2_daily_symbol_checkpoints" in sql:
                    return [(
                        "000001", "succeeded", 1, Decimal("10"), None, None,
                        sha256([primary]), sha256([adjusted]), sha256([fact]),
                        sha256([verification]), None,
                    )]
                if "FROM m2_daily_primary_bars" in sql:
                    return [(
                        primary["symbol"], TARGET, primary["source"], primary["exchange"],
                        Decimal(primary["open"]), Decimal(primary["high"]), Decimal(primary["low"]),
                        Decimal(primary["close"]), None, primary["volume_shares"],
                        Decimal(primary["amount_cny"]), Decimal(primary["turnover_percent"]),
                        primary["trade_status"], None, primary["adjustment"],
                        primary["schema_version"], primary_hash,
                    )]
                if "FROM m2_daily_adjusted_bars" in sql:
                    return [(
                        adjusted["symbol"], TARGET, adjusted["exchange"], adjusted["index_code"],
                        *[Decimal(adjusted[key]) for key in ("open", "high", "low", "close", "previous_close")],
                        adjusted["volume_shares"], Decimal(adjusted["amount_cny"]),
                        Decimal(adjusted["turnover_percent"]), Decimal(adjusted["qfq_factor"]),
                        Decimal(adjusted["hfq_factor"]),
                        *[Decimal(adjusted[key]) for key in (
                            "qfq_open", "qfq_high", "qfq_low", "qfq_close",
                            "hfq_open", "hfq_high", "hfq_low", "hfq_close",
                        )],
                        adjusted["primary_source"], adjusted["factor_source"],
                        adjusted["schema_version"], sha256(adjusted),
                    )]
                if "FROM m2_daily_tradeability_facts" in sql:
                    return [(
                        fact["symbol"], TARGET, fact["index_code"], 1, 1, 0, 0,
                        fact["listing_age_sessions"], Decimal(fact["limit_rate"]),
                        Decimal(fact["limit_up"]), Decimal(fact["limit_down"]),
                        0, 0, 0, 0, 1, 1, "[]", fact["schema_version"], sha256(fact),
                    )]
                if "FROM m2_daily_verification_bars" in sql:
                    return [(
                        verification["symbol"], TARGET, verification["source"], verification["exchange"],
                        Decimal(verification["open"]), Decimal(verification["high"]),
                        Decimal(verification["low"]), Decimal(verification["close"]), None,
                        verification["volume_shares"], Decimal(verification["amount_cny"]),
                        Decimal(verification["turnover_percent"]), verification["trade_status"],
                        None, verification["adjustment"], verification["schema_version"],
                        sha256(verification),
                    )]
                if "FROM m2_daily_adjustment_events" in sql:
                    return []
                return []
            return router

        loaded, metadata = load_daily_checkpoint_evidence(
            FakeConnection(router_with_primary_hash(sha256(primary))), "daily-scope",
        )
        self.assertEqual(loaded.primary_bars, [primary])
        self.assertEqual(metadata["succeeded_symbols"], ["000001"])
        with self.assertRaisesRegex(RuntimeError, "primary row hash mismatch"):
            load_daily_checkpoint_evidence(
                FakeConnection(router_with_primary_hash("0" * 64)), "daily-scope",
            )

    def test_latest_lineage_falls_back_to_accepted_research_history(self) -> None:
        def router(sql: str, params: Any):
            if "FROM m2_daily_runs" in sql:
                return []
            if "FROM m2_history_runs" in sql:
                return [(PREVIOUS, 1, 0, 0)]
            return []

        self.assertEqual(
            latest_accepted_lineage(FakeConnection(router), "base"),
            (PREVIOUS, "base"),
        )

    def test_latest_lineage_requires_an_unbroken_predecessor_chain(self) -> None:
        next_session = date(2026, 7, 28)

        def valid_router(sql: str, params: Any):
            if "FROM m2_history_runs" in sql:
                return [(PREVIOUS, 1, 0, 0)]
            if "FROM m2_daily_runs" in sql:
                return [
                    ("daily-27", TARGET, PREVIOUS, "base", 0, 0),
                    ("daily-28", next_session, TARGET, "daily-27", 0, 0),
                ]
            return []

        self.assertEqual(
            latest_accepted_lineage(FakeConnection(valid_router), "base"),
            (next_session, "daily-28"),
        )

        def broken_router(sql: str, params: Any):
            if "FROM m2_history_runs" in sql:
                return [(PREVIOUS, 1, 0, 0)]
            if "FROM m2_daily_runs" in sql:
                return [("daily-28", next_session, TARGET, "missing-daily-27", 0, 0)]
            return []

        with self.assertRaisesRegex(RuntimeError, "gap or predecessor mismatch"):
            latest_accepted_lineage(FakeConnection(broken_router), "base")

    def test_target_selection_never_skips_a_missing_session(self) -> None:
        sessions = (PREVIOUS, TARGET, date(2026, 7, 28))
        self.assertEqual(_select_target(sessions, PREVIOUS, date(2026, 7, 28), None), TARGET)
        with self.assertRaisesRegex(RuntimeError, "cannot skip"):
            _select_target(sessions, PREVIOUS, date(2026, 7, 28), date(2026, 7, 28))


class FakePrimary:
    def __init__(self, *, event: AdjustmentEvent | None = None) -> None:
        self.event = event

    def fetch_raw_with_fallback(self, symbol: str, start: date, end: date):
        return {TARGET: raw_bar(symbol)}, "akshare_eastmoney"

    def fetch_sina_adjustments(self, symbol: str, end: date):
        return [] if self.event is None else [self.event]


class FakeVerification:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def fetch_raw(self, symbol: str, start: date, end: date, *, exclude_sources=None):
        if self.fail:
            raise ConnectionError("verification offline")
        return [raw_bar(symbol, source="akshare_sina")]


class FakeSecondary:
    def __init__(self, *, preclose: str = "10", status: str = "1") -> None:
        self.preclose = preclose
        self.status = status

    def fetch_status(self, symbol: str, start: date, end: date):
        return {TARGET: {"tradestatus": self.status, "isST": "0", "preclose": self.preclose}}


class DailyCaptureTests(unittest.TestCase):
    def test_capture_accepts_normal_and_verified_corporate_action_sessions(self) -> None:
        kwargs = {
            "plan": plan(), "symbol": "000001", "verification_source": FakeVerification(),
            "previous_states": {"000001": previous_state()},
            "ipo_dates": {"000001": date(1991, 4, 3)},
            "calendar_dates": (PREVIOUS, TARGET),
        }
        normal, reported, status, error = capture_symbol(
            **kwargs, primary_source=FakePrimary(), secondary_source=FakeSecondary(),
        )
        self.assertEqual((reported, status, error), (Decimal("10"), "succeeded", None))
        self.assertEqual(len(normal.adjusted_bars), 1)

        event = AdjustmentEvent(
            "000001", TARGET, Decimal("1"), Decimal("1.052632"),
            source="akshare_sina_factor_multiplicative",
        )
        corporate, _reported, status, error = capture_symbol(
            **kwargs, primary_source=FakePrimary(event=event),
            secondary_source=FakeSecondary(preclose="9.5"),
        )
        self.assertEqual((status, error), ("succeeded", None))
        self.assertEqual(corporate.adjustments[0]["effective_date"], TARGET.isoformat())
        self.assertEqual(corporate.adjusted_bars[0]["hfq_factor"], "1.052632")

    def test_capture_blocks_missing_predecessor_or_verification_without_guessing(self) -> None:
        common = {
            "plan": plan(), "symbol": "000001", "primary_source": FakePrimary(),
            "secondary_source": FakeSecondary(), "ipo_dates": {"000001": date(1991, 4, 3)},
            "calendar_dates": (PREVIOUS, TARGET),
        }
        missing, _reported, status, error = capture_symbol(
            **common, verification_source=FakeVerification(), previous_states={},
        )
        self.assertEqual(status, "blocked")
        self.assertIn("predecessor", str(error))
        self.assertEqual(missing.primary_bars, [])
        self.assertFalse(missing.tradeability[0]["can_buy"])

        unverified, _reported, status, error = capture_symbol(
            **common, verification_source=FakeVerification(fail=True),
            previous_states={"000001": previous_state()},
        )
        self.assertEqual(status, "blocked")
        self.assertIn("verification offline", str(error))
        self.assertEqual(len(unverified.primary_bars), 1)
        self.assertEqual(unverified.verification_bars, [])

    def test_confirmed_suspension_is_complete_and_never_fabricates_a_bar(self) -> None:
        evidence, _reported, status, error = capture_symbol(
            plan=plan(), symbol="000001", primary_source=FakePrimary(),
            verification_source=FakeVerification(), secondary_source=FakeSecondary(status="0"),
            previous_states={}, ipo_dates={"000001": date(1991, 4, 3)},
            calendar_dates=(PREVIOUS, TARGET),
        )
        self.assertEqual((status, error), ("succeeded", None))
        self.assertEqual(evidence.primary_bars, [])
        self.assertTrue(evidence.tradeability[0]["is_suspended"])
        self.assertFalse(evidence.tradeability[0]["can_buy"])
        self.assertFalse(evidence.tradeability[0]["can_sell"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import call, patch
from zoneinfo import ZoneInfo

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import DailyBar
from scripts.market_data.daily_adjustments import PreviousAdjustedState, build_daily_adjusted_bars
from scripts.market_data.daily_incremental import DailyIncrementalPlan
from scripts.market_data.daily_incremental_runner import (
    DailyPrerequisiteTimeout,
    _accepted_replay_result,
    _factor_reference_closes,
    _select_target,
    capture_symbol,
    run,
)
from scripts.market_data.historical_contracts import AdjustmentEvent
from scripts.market_data.manifest import sha256
from scripts.market_data.sources.akshare_history_source import SinaFactorsUnavailableError
from scripts.market_data.sources.tencent_history_source import TencentHistorySource
from scripts.market_data.tidb_daily_store import (
    DailyEvidence,
    _canonical_adjusted_bar,
    canonical_lineage_evidence,
    connect,
    default_daily_dataset_id,
    daily_correction_context,
    ensure_daily_schema,
    latest_accepted_lineage,
    load_daily_checkpoint_evidence,
    load_previous_adjusted_states,
    publish_daily_run,
    publish_daily_symbol_checkpoint,
    recover_compatible_daily_checkpoints,
    recovered_previous_states_from_lineage,
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
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


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


def corporate_action_record(
    symbol: str = "000001",
    *,
    cash_per_ten: str = "5.000000",
    bonus_ratio: str | None = None,
    conversion_ratio: str | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "ex_dividend_date": TARGET.isoformat(),
        "equity_record_date": PREVIOUS.isoformat(),
        "report_date": "2025-12-31",
        "notice_date": "2026-07-20",
        "assign_progress": "实施方案",
        "cash_per_ten_shares": cash_per_ten,
        "bonus_ratio": bonus_ratio,
        "conversion_ratio": conversion_ratio,
        "plan_profile": f"10派{Decimal(cash_per_ten)}元",
    }


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
    def test_transient_tidb_connect_error_retries_and_recovers(self) -> None:
        connection = object()
        with patch(
            "scripts.market_data.tidb_daily_store._connect_once",
            side_effect=[TimeoutError("TLS read timed out"), connection],
        ) as connect_once, patch("scripts.market_data.tidb_daily_store.time.sleep") as sleep:
            self.assertIs(connect(object()), connection)

        self.assertEqual(connect_once.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_transient_tidb_connect_errors_exhaust_three_bounded_attempts(self) -> None:
        errors = [
            ConnectionResetError("connection reset"),
            RuntimeError(2003, "cannot connect"),
            RuntimeError(2013, "lost connection"),
        ]
        with patch(
            "scripts.market_data.tidb_daily_store._connect_once",
            side_effect=errors,
        ) as connect_once, patch("scripts.market_data.tidb_daily_store.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "lost connection"):
                connect(object())

        self.assertEqual(connect_once.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_permanent_tidb_connect_error_fails_without_retry(self) -> None:
        with patch(
            "scripts.market_data.tidb_daily_store._connect_once",
            side_effect=RuntimeError(1045, "access denied"),
        ) as connect_once, patch("scripts.market_data.tidb_daily_store.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "access denied"):
                connect(object())

        connect_once.assert_called_once()
        sleep.assert_not_called()

    def test_prerequisite_deadline_cannot_be_swallowed_by_vendor_handlers(self) -> None:
        self.assertTrue(issubclass(DailyPrerequisiteTimeout, BaseException))
        self.assertFalse(issubclass(DailyPrerequisiteTimeout, Exception))

        @contextmanager
        def immediate_deadline(_seconds: int):
            raise DailyPrerequisiteTimeout("fixture calendar deadline")
            yield

        with patch(
            "scripts.market_data.daily_incremental_runner.prerequisite_deadline",
            immediate_deadline,
        ):
            with self.assertRaisesRegex(DailyPrerequisiteTimeout, "fixture calendar deadline"):
                run(
                    observed_at=datetime(2026, 7, 28, 17, 0, tzinfo=SHANGHAI),
                    base_history_dataset_id="base-history",
                    output_dir=Path("unused-daily-output"),
                    requested_target=TARGET,
                )

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
            "m2_daily_lineage_evidence",
            "m2_daily_run_supersessions",
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
            "verification_bars": 1, "adjustments": 0, "lineage_evidence": 0,
            "symbol_checkpoints": 1,
        })
        deletes = [sql for sql, _params in connection.executed if sql.strip().startswith("DELETE")]
        self.assertEqual(len(deletes), 6)
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

    def test_adjusted_checkpoint_hash_uses_tidb_decimal_precision(self) -> None:
        evidence = complete_evidence()
        evidence.adjusted_bars[0]["previous_close"] = "9.5"
        connection = FakeConnection()
        publish_daily_symbol_checkpoint(
            connection, evidence, dataset_id="daily-scope", symbol="000001",
            target_session=TARGET, verification_required=True,
            reported_previous_close=Decimal("9.5"), status="succeeded",
        )
        adjusted_batch = next(
            rows for sql, rows in connection.executed_many
            if "m2_daily_adjusted_bars" in sql
        )
        checkpoint_batch = next(
            rows for sql, rows in connection.executed_many
            if "m2_daily_symbol_checkpoints" in sql
        )
        canonical = _canonical_adjusted_bar(evidence.adjusted_bars[0])
        self.assertEqual(canonical["previous_close"], "9.5000")
        self.assertEqual(adjusted_batch[0][9], "9.5000")
        self.assertEqual(adjusted_batch[0][-1], sha256(canonical))
        self.assertEqual(checkpoint_batch[0][14], sha256([canonical]))

    def test_blocked_checkpoint_retains_primary_bar_without_adjusted_bar(self) -> None:
        evidence = complete_evidence()
        evidence.adjusted_bars.clear()
        evidence.verification_bars.clear()
        evidence.tradeability[0]["can_buy"] = False
        evidence.tradeability[0]["can_sell"] = False
        connection = FakeConnection()
        counts = publish_daily_symbol_checkpoint(
            connection, evidence, dataset_id="daily-scope", symbol="000001",
            target_session=TARGET, verification_required=True,
            reported_previous_close=Decimal("10"), status="blocked",
            error=RuntimeError("missing adjustment factor"),
        )
        self.assertEqual(counts["primary_bars"], 1)
        self.assertEqual(counts["adjusted_bars"], 0)
        self.assertEqual(counts["verification_bars"], 0)
        checkpoint_batch = next(
            rows for sql, rows in connection.executed_many
            if "m2_daily_symbol_checkpoints" in sql
        )
        self.assertEqual(checkpoint_batch[0][4:9], (1, 0, 1, 1, 0))

    def test_lineage_evidence_is_canonical_persisted_and_rehydrates_predecessor(self) -> None:
        evidence = complete_evidence()
        lineage = canonical_lineage_evidence({
            "symbol": "000001",
            "target_session": TARGET.isoformat(),
            "kind": "gap_no_adjustment_recovery",
            "source": "tencent_raw_hfq_continuity",
            "details": {
                "prior_session": "2026-07-23",
                "recovered_session": PREVIOUS.isoformat(),
                "prior_source_dataset_id": "base-history",
                "accepted_prior_close": "9.8000",
                "recovered_raw_close": "10.0000",
                "observed_sessions": ["2026-07-23", PREVIOUS.isoformat()],
                "maximum_implied_hfq_change_rate": "0.001",
                "qfq_factor": "1.000000",
                "hfq_factor": "2.000000",
                "raw_rows_sha256": "b" * 64,
                "hfq_rows_sha256": "c" * 64,
            },
        })
        evidence.lineage_evidence.append(lineage)
        counts = publish_daily_symbol_checkpoint(
            FakeConnection(), evidence, dataset_id="daily-scope", symbol="000001",
            target_session=TARGET, verification_required=True,
            reported_previous_close=Decimal("10"), status="succeeded",
        )
        self.assertEqual(counts["lineage_evidence"], 1)
        states = recovered_previous_states_from_lineage(
            [lineage], previous_session=PREVIOUS,
        )
        self.assertEqual(states["000001"].raw_close, Decimal("10.0000"))
        self.assertEqual(states["000001"].hfq_factor, Decimal("2.000000"))

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
            "lineage_evidence_count": len(evidence.lineage_evidence),
            "primary_sha256": sha256(evidence.primary_bars),
            "adjusted_sha256": sha256([
                _canonical_adjusted_bar(row) for row in evidence.adjusted_bars
            ]),
            "tradeability_sha256": sha256(evidence.tradeability),
            "verification_sha256": sha256(evidence.verification_bars),
            "adjustments_sha256": sha256(evidence.adjustments),
            "lineage_evidence_sha256": sha256(evidence.lineage_evidence),
        }
        manifest = {
            **evidence.manifest, **values,
            "accepted": True, "manifest_version": "m2-daily-incremental-manifest-v6",
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
            if "run.target_session=%s" in sql:
                return [(dataset_id, manifest_hash)]
            return []

        replay = publish_daily_run(
            FakeConnection(replay_router), publication, dataset_id=dataset_id,
            base_history_dataset_id="base", predecessor_dataset_id="base",
        )
        self.assertTrue(replay["idempotent_replay"])

    def test_correction_preserves_old_run_and_registers_one_idempotent_replacement(self) -> None:
        evidence = complete_evidence()
        scope_hash = "9" * 64
        dataset_id = default_daily_dataset_id(TARGET, scope_hash)
        manifest = {
            **evidence.manifest,
            "accepted": True,
            "manifest_version": "m2-daily-incremental-manifest-v6",
            "target_session": TARGET.isoformat(),
            "previous_session": PREVIOUS.isoformat(),
            "snapshot_effective_session": PREVIOUS.isoformat(),
            "scope_sha256": scope_hash,
            "expected_symbol_count": 1,
            "quality_sha256": "e" * 64,
            "supersedes_dataset_id": "bad-daily-run",
            "correction_reason": "corporate_action_inventory_false_green",
            "primary_row_count": len(evidence.primary_bars),
            "adjusted_row_count": len(evidence.adjusted_bars),
            "tradeability_row_count": len(evidence.tradeability),
            "verification_row_count": len(evidence.verification_bars),
            "adjustment_event_count": len(evidence.adjustments),
            "lineage_evidence_count": len(evidence.lineage_evidence),
            "primary_sha256": sha256(evidence.primary_bars),
            "adjusted_sha256": sha256([
                _canonical_adjusted_bar(row) for row in evidence.adjusted_bars
            ]),
            "tradeability_sha256": sha256(evidence.tradeability),
            "verification_sha256": sha256(evidence.verification_bars),
            "adjustments_sha256": sha256(evidence.adjustments),
            "lineage_evidence_sha256": sha256(evidence.lineage_evidence),
        }
        publication = DailyEvidence(
            manifest=manifest,
            primary_bars=evidence.primary_bars,
            tradeability=evidence.tradeability,
            verification_bars=evidence.verification_bars,
            adjusted_bars=evidence.adjusted_bars,
            adjustments=evidence.adjustments,
        )

        def router(sql: str, params: Any):
            if "SELECT symbol, status" in sql:
                return [("000001", "succeeded")]
            if "run.target_session=%s" in sql:
                return [("bad-daily-run", "old-manifest-hash")]
            return []

        connection = FakeConnection(router)
        result = publish_daily_run(
            connection,
            publication,
            dataset_id=dataset_id,
            base_history_dataset_id="base",
            predecessor_dataset_id="base",
            supersedes_dataset_id="bad-daily-run",
            correction_reason="corporate_action_inventory_false_green",
        )
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(result["superseded_dataset_id"], "bad-daily-run")
        supersession_inserts = [
            (sql, params) for sql, params in connection.executed
            if "INSERT INTO m2_daily_run_supersessions" in sql
        ]
        self.assertEqual(len(supersession_inserts), 1)
        self.assertFalse(any("UPDATE m2_daily_runs" in sql for sql, _params in connection.executed))

        manifest_hash = sha256(manifest)

        def replay_router(sql: str, params: Any):
            if "SELECT symbol, status" in sql:
                return [("000001", "succeeded")]
            if "JOIN m2_daily_runs AS run" in sql and "superseded_dataset_id=%s" in sql:
                return [(dataset_id, manifest_hash)]
            return []

        replay = publish_daily_run(
            FakeConnection(replay_router),
            publication,
            dataset_id=dataset_id,
            base_history_dataset_id="base",
            predecessor_dataset_id="base",
            supersedes_dataset_id="bad-daily-run",
            correction_reason="corporate_action_inventory_false_green",
        )
        self.assertTrue(replay["idempotent_replay"])

    def test_correction_context_allows_only_active_lineage_tip(self) -> None:
        base_session = date(2026, 7, 23)

        def valid_router(sql: str, params: Any):
            if "SELECT business_end" in sql:
                return [(base_session, 1, 0, 0)]
            if "SELECT run.dataset_id, run.target_session" in sql:
                return [
                    ("daily-previous", PREVIOUS, base_session, "base", 0, 0),
                    ("bad-daily-run", TARGET, PREVIOUS, "daily-previous", 0, 0),
                ]
            if "WHERE run.dataset_id=%s" in sql:
                return [(
                    TARGET, PREVIOUS, "daily-previous", "base", 1, 0, 0, None,
                )]
            return []

        self.assertEqual(
            daily_correction_context(
                FakeConnection(valid_router),
                base_history_dataset_id="base",
                superseded_dataset_id=" bad-daily-run \t",
                target_session=TARGET,
            ),
            (PREVIOUS, "daily-previous"),
        )

        def downstream_router(sql: str, params: Any):
            if "SELECT business_end" in sql:
                return [(base_session, 1, 0, 0)]
            if "SELECT run.dataset_id, run.target_session" in sql:
                return [
                    ("daily-previous", PREVIOUS, base_session, "base", 0, 0),
                    ("bad-daily-run", TARGET, PREVIOUS, "daily-previous", 0, 0),
                    ("later-run", date(2026, 7, 28), TARGET, "bad-daily-run", 0, 0),
                ]
            return []

        with self.assertRaisesRegex(RuntimeError, "lineage tip"):
            daily_correction_context(
                FakeConnection(downstream_router),
                base_history_dataset_id="base",
                superseded_dataset_id="bad-daily-run",
                target_session=TARGET,
            )

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
        adjusted = _canonical_adjusted_bar(evidence.adjusted_bars[0])
        fact = evidence.tradeability[0]
        verification = evidence.verification_bars[0]

        def router_with_primary_hash(primary_hash: str):
            def router(sql: str, params: Any):
                if "FROM m2_daily_symbol_checkpoints" in sql:
                    return [(
                        "000001", "succeeded", 1, Decimal("10"), None, None,
                        None, None, None,
                        sha256([primary]), sha256([adjusted]), sha256([fact]),
                        sha256([verification]), None, None,
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
                        fact["listing_age_sessions"], Decimal(fact["limit_rate"]).quantize(Decimal("0.000001")),
                        Decimal(fact["limit_up"]).quantize(Decimal("0.0001")),
                        Decimal(fact["limit_down"]).quantize(Decimal("0.0001")),
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
        ordered_connection = FakeConnection(router_with_primary_hash(sha256(primary)))
        load_daily_checkpoint_evidence(ordered_connection, "daily-scope")
        market_row_queries = [
            sql for sql, _params in ordered_connection.executed
            if "FROM m2_daily_primary_bars" in sql
            or "FROM m2_daily_verification_bars" in sql
        ]
        self.assertEqual(len(market_row_queries), 2)
        self.assertTrue(all(
            "ORDER BY source, symbol, business_date" in sql
            for sql in market_row_queries
        ))
        with self.assertRaisesRegex(RuntimeError, "primary row hash mismatch"):
            load_daily_checkpoint_evidence(
                FakeConnection(router_with_primary_hash("0" * 64)), "daily-scope",
            )

    def test_recovery_prefers_valid_eastmoney_evidence_and_preserves_origin(self) -> None:
        empty = DailyEvidence(
            manifest={"authoritative": False, "simulation_orders_allowed": False},
            primary_bars=[], tradeability=[], verification_bars=[],
            adjusted_bars=[], adjustments=[],
        )
        empty_metadata = {
            "succeeded_symbols": [], "blocked_symbols": [],
            "verification_required_symbols": [], "reported_previous_closes": {},
            "status_sources": {}, "reported_previous_close_sources": {},
            "checkpoint_origin_dataset_ids": {}, "errors": {},
        }
        eastmoney = complete_evidence()
        sina = complete_evidence()
        sina.primary_bars[0]["source"] = "akshare_sina"
        sina.adjusted_bars[0]["primary_source"] = "akshare_sina"

        def metadata(source: str) -> dict[str, Any]:
            return {
                **empty_metadata,
                "succeeded_symbols": ["000001"],
                "verification_required_symbols": ["000001"],
                "reported_previous_closes": {"000001": Decimal("10")},
                "status_sources": {"000001": "baostock_daily_status"},
                "reported_previous_close_sources": {
                    "000001": "baostock_reported_preclose",
                },
            }

        def load(_connection: Any, dataset_id: str):
            if dataset_id == "stable":
                return empty, empty_metadata
            if dataset_id == "corrupt":
                raise RuntimeError("fixture hash mismatch")
            if dataset_id == "old-sina":
                return sina, metadata("akshare_sina")
            if dataset_id == "new-eastmoney":
                return eastmoney, metadata("akshare_eastmoney")
            raise AssertionError(dataset_id)

        connection = FakeConnection()
        with (
            patch(
                "scripts.market_data.tidb_daily_store.load_daily_checkpoint_evidence",
                side_effect=load,
            ),
            patch(
                "scripts.market_data.tidb_daily_store._query_all",
                return_value=[("corrupt",), ("new-eastmoney",), ("old-sina",)],
            ),
        ):
            result = recover_compatible_daily_checkpoints(
                connection, dataset_id="stable", target_session=TARGET,
                expected_membership={"000001": "000300"},
                verification_symbols=("000001",),
                previous_states={"000001": previous_state()},
            )

        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["recovered_by_source_dataset"], {"new-eastmoney": 1})
        self.assertIn("fixture hash mismatch", result["rejected_datasets"]["corrupt"])
        checkpoint_batches = [
            rows for sql, rows in connection.executed_many
            if "m2_daily_symbol_checkpoints" in sql
        ]
        self.assertEqual(len(checkpoint_batches), 1)
        self.assertEqual(checkpoint_batches[0][0][12], "new-eastmoney")
        primary_batches = [
            rows for sql, rows in connection.executed_many
            if "m2_daily_primary_bars" in sql
        ]
        self.assertEqual(primary_batches[0][0][3], "akshare_eastmoney")
        self.assertEqual(connection.commits, 1)

        excluded = recover_compatible_daily_checkpoints(
            FakeConnection(), dataset_id="stable", target_session=TARGET,
            expected_membership={"000001": "000300"},
            verification_symbols=("000001",),
            previous_states={"000001": previous_state()},
            existing_metadata=empty_metadata,
            excluded_symbols=("000001",),
        )
        self.assertEqual(excluded["recovered"], 0)
        self.assertEqual(excluded["candidate_datasets"], 0)

        incompatible = PreviousAdjustedState(
            symbol="000001", business_date=PREVIOUS, raw_close=Decimal("9"),
            qfq_factor=Decimal("1"), hfq_factor=Decimal("1"),
            source_dataset_id="different-lineage",
        )
        rejected_connection = FakeConnection()
        with (
            patch(
                "scripts.market_data.tidb_daily_store.load_daily_checkpoint_evidence",
                side_effect=load,
            ),
            patch(
                "scripts.market_data.tidb_daily_store._query_all",
                return_value=[("new-eastmoney",)],
            ),
        ):
            rejected = recover_compatible_daily_checkpoints(
                rejected_connection, dataset_id="stable", target_session=TARGET,
                expected_membership={"000001": "000300"},
                verification_symbols=("000001",),
                previous_states={"000001": incompatible},
            )
        self.assertEqual(rejected["recovered"], 0)
        self.assertEqual(rejected_connection.commits, 0)

        bad_verification = complete_evidence()
        bad_verification.verification_bars[0]["volume_shares"] = 1_000_000

        def load_bad_verification(_connection: Any, dataset_id: str):
            if dataset_id == "stable":
                return empty, empty_metadata
            if dataset_id == "bad-verification":
                return bad_verification, metadata("akshare_eastmoney")
            raise AssertionError(dataset_id)

        rejected_verification_connection = FakeConnection()
        with (
            patch(
                "scripts.market_data.tidb_daily_store.load_daily_checkpoint_evidence",
                side_effect=load_bad_verification,
            ),
            patch(
                "scripts.market_data.tidb_daily_store._query_all",
                return_value=[("bad-verification",)],
            ),
        ):
            rejected_verification = recover_compatible_daily_checkpoints(
                rejected_verification_connection, dataset_id="stable", target_session=TARGET,
                expected_membership={"000001": "000300"},
                verification_symbols=("000001",),
                previous_states={"000001": previous_state()},
            )
        self.assertEqual(rejected_verification["recovered"], 0)
        self.assertEqual(rejected_verification_connection.commits, 0)

    def test_recovery_does_not_copy_excluded_symbols_from_mixed_candidate_evidence(self) -> None:
        empty = DailyEvidence(
            manifest={"authoritative": False, "simulation_orders_allowed": False},
            primary_bars=[], tradeability=[], verification_bars=[], adjusted_bars=[], adjustments=[],
        )
        empty_metadata = {
            "succeeded_symbols": [], "blocked_symbols": [],
            "verification_required_symbols": [], "reported_previous_closes": {},
            "status_sources": {}, "reported_previous_close_sources": {},
            "checkpoint_origin_dataset_ids": {}, "errors": {},
        }
        eligible = complete_evidence()
        excluded_evidence = complete_evidence()
        for rows in (
            excluded_evidence.primary_bars, excluded_evidence.adjusted_bars,
            excluded_evidence.tradeability, excluded_evidence.verification_bars,
        ):
            rows[0]["symbol"] = "000002"
        mixed = DailyEvidence(
            manifest={"authoritative": False, "simulation_orders_allowed": False},
            primary_bars=eligible.primary_bars + excluded_evidence.primary_bars,
            tradeability=eligible.tradeability + excluded_evidence.tradeability,
            verification_bars=eligible.verification_bars + excluded_evidence.verification_bars,
            adjusted_bars=eligible.adjusted_bars + excluded_evidence.adjusted_bars,
            adjustments=[],
        )
        mixed_metadata = {
            **empty_metadata,
            "succeeded_symbols": ["000001", "000002"],
            "verification_required_symbols": ["000001", "000002"],
            "reported_previous_closes": {"000001": Decimal("10"), "000002": Decimal("10")},
            "status_sources": {"000001": "baostock_daily_status", "000002": "baostock_daily_status"},
            "reported_previous_close_sources": {
                "000001": "baostock_reported_preclose", "000002": "baostock_reported_preclose",
            },
        }
        connection = FakeConnection()
        with (
            patch(
                "scripts.market_data.tidb_daily_store.load_daily_checkpoint_evidence",
                side_effect=lambda _connection, dataset_id: (empty, empty_metadata)
                if dataset_id == "stable" else (mixed, mixed_metadata),
            ),
            patch("scripts.market_data.tidb_daily_store._query_all", return_value=[("mixed",)]),
        ):
            result = recover_compatible_daily_checkpoints(
                connection, dataset_id="stable", target_session=TARGET,
                expected_membership={"000001": "000300", "000002": "000300"},
                verification_symbols=("000001", "000002"),
                previous_states={
                    "000001": previous_state(),
                    "000002": replace(previous_state(), symbol="000002"),
                },
                excluded_symbols=("000002",),
            )

        self.assertEqual(result["recovered"], 1)
        checkpoint_batches = [
            rows for sql, rows in connection.executed_many
            if "m2_daily_symbol_checkpoints" in sql
        ]
        self.assertEqual([row[1] for row in checkpoint_batches[0]], ["000001"])

    def test_tradeability_precision_loss_is_rejected_before_publication(self) -> None:
        evidence = complete_evidence()
        invalid_fact = {**evidence.tradeability[0], "limit_rate": "0.100001"}
        invalid = DailyEvidence(
            manifest=evidence.manifest, primary_bars=evidence.primary_bars,
            tradeability=[invalid_fact], verification_bars=evidence.verification_bars,
            adjusted_bars=evidence.adjusted_bars, adjustments=evidence.adjustments,
        )
        connection = FakeConnection()
        with self.assertRaisesRegex(ValueError, "two-decimal tradeability contract"):
            publish_daily_symbol_checkpoint(
                connection, invalid, dataset_id="daily-scope", symbol="000001",
                target_session=TARGET, verification_required=True,
                reported_previous_close=Decimal("10"), status="succeeded",
            )
        self.assertEqual(connection.executed, [])

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

    def test_correction_tip_error_reports_actual_and_requested_tip(self) -> None:
        active_session = date(2026, 7, 31)
        requested_session = date(2026, 8, 3)

        def router(sql: str, params: Any):
            if "FROM m2_history_runs" in sql:
                return [(PREVIOUS, 1, 0, 0)]
            if "FROM m2_daily_runs" in sql:
                return [("active-daily", active_session, PREVIOUS, "base", 0, 0)]
            return []

        with self.assertRaisesRegex(
            RuntimeError,
            r"active tip 2026-07-31/'active-daily' \(len=12\); requested 2026-08-03/'requested-daily' \(len=15\)",
        ):
            daily_correction_context(
                FakeConnection(router),
                base_history_dataset_id="base",
                superseded_dataset_id="requested-daily",
                target_session=requested_session,
            )

    def test_target_selection_never_skips_a_missing_session(self) -> None:
        sessions = (PREVIOUS, TARGET, date(2026, 7, 28))
        self.assertEqual(_select_target(sessions, PREVIOUS, date(2026, 7, 28), None), TARGET)
        with self.assertRaisesRegex(RuntimeError, "cannot skip"):
            _select_target(sessions, PREVIOUS, date(2026, 7, 28), date(2026, 7, 28))

    def test_runner_union_discovery_rejects_provider_missing_target_before_acquisition(self) -> None:
        future = date(2026, 7, 28)
        primary = TradingCalendar.build(
            "primary", date(2026, 7, 1), future, (PREVIOUS, TARGET, future),
        )
        secondary = TradingCalendar.build(
            "secondary", date(2026, 7, 1), future, (PREVIOUS, future),
        )
        connection = FakeConnection()
        with (
            patch(
                "scripts.market_data.daily_incremental_runner.load_calendars",
                return_value=(primary, secondary, [], {}),
            ),
            patch("scripts.market_data.daily_incremental_runner.TiDBConfig.from_env"),
            patch("scripts.market_data.daily_incremental_runner.connect", return_value=connection),
            patch(
                "scripts.market_data.daily_incremental_runner.latest_accepted_lineage",
                return_value=(PREVIOUS, "accepted-previous"),
            ),
            patch(
                "scripts.market_data.daily_incremental_runner.EastmoneyCorporateActionSource",
                side_effect=AssertionError("calendar rejection must precede acquisition"),
            ) as corporate_source,
        ):
            with self.assertRaisesRegex(RuntimeError, rf"{TARGET.isoformat()}.*secondary"):
                run(
                    observed_at=datetime(2026, 7, 28, 17, 0, tzinfo=SHANGHAI),
                    base_history_dataset_id="base-history",
                    output_dir=Path("unused-daily-output"),
                )

        corporate_source.assert_not_called()

    def test_exact_accepted_target_returns_immutable_idempotent_replay(self) -> None:
        result = _accepted_replay_result(
            TARGET,
            "accepted-daily",
            TARGET,
            base_history_dataset_id="base-history",
        )
        self.assertEqual(result, {
            "event": "daily_accepted",
            "dataset_id": "accepted-daily",
            "target_session": TARGET.isoformat(),
            "accepted": True,
            "idempotent_replay": True,
            "authoritative": False,
            "simulation_orders_allowed": False,
        })
        self.assertIsNone(_accepted_replay_result(
            TARGET, "accepted-daily", None, base_history_dataset_id="base-history",
        ))
        self.assertIsNone(_accepted_replay_result(
            TARGET,
            "accepted-daily",
            date(2026, 7, 28),
            base_history_dataset_id="base-history",
        ))
        self.assertIsNone(_accepted_replay_result(
            TARGET, "base-history", TARGET, base_history_dataset_id="base-history",
        ))

    def test_runner_returns_accepted_daily_replay_before_acquisition(self) -> None:
        calendar = SimpleNamespace(
            open_dates=(PREVIOUS, TARGET),
            end_date=date(2026, 7, 28),
        )
        connection = FakeConnection()
        with (
            patch(
                "scripts.market_data.daily_incremental_runner.load_calendars",
                return_value=(calendar, calendar, [], {}),
            ),
            patch("scripts.market_data.daily_incremental_runner.TiDBConfig.from_env"),
            patch("scripts.market_data.daily_incremental_runner.connect", return_value=connection),
            patch(
                "scripts.market_data.daily_incremental_runner.latest_accepted_lineage",
                return_value=(TARGET, "accepted-daily"),
            ),
            patch(
                "scripts.market_data.daily_incremental_runner.EastmoneyCorporateActionSource",
                side_effect=AssertionError("accepted replay must not acquire source data"),
            ) as corporate_source,
        ):
            result = run(
                observed_at=datetime(2026, 7, 28, 17, 0, tzinfo=SHANGHAI),
                base_history_dataset_id="base-history",
                output_dir=Path("unused-daily-output"),
                requested_target=TARGET,
            )

        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(result["dataset_id"], "accepted-daily")
        corporate_source.assert_not_called()


class FakePrimary:
    timeout_seconds = 1
    attempts = 1

    def __init__(self, *, event: AdjustmentEvent | None = None) -> None:
        self.event = event

    def fetch_raw_with_fallback(self, symbol: str, start: date, end: date):
        return {TARGET: raw_bar(symbol)}, "akshare_eastmoney"

    def fetch_daily_raw_with_reference(self, symbol: str, previous: date, target: date):
        return (
            {TARGET: raw_bar(symbol)},
            "akshare_eastmoney",
            Decimal("10"),
            "akshare_eastmoney_change_amount",
        )

    def fetch_sina_adjustments(self, symbol: str, end: date):
        return [] if self.event is None else [self.event]


class FakeSinaPrimary(FakePrimary):
    timeout_seconds = 1
    attempts = 1

    def fetch_daily_raw_with_reference(self, symbol: str, previous: date, target: date):
        return (
            {TARGET: raw_bar(symbol, source="akshare_sina")},
            "akshare_sina",
            Decimal("10"),
            "akshare_sina_exact_predecessor_close",
        )

    def fetch_sina_adjustments(self, symbol: str, end: date):
        raise SinaFactorsUnavailableError(
            f"AKShare Sina confirmed both factor series unavailable for {symbol}"
        )


class FakeDividendSinaPrimary(FakeSinaPrimary):
    def fetch_sina_adjustments(self, symbol: str, end: date):
        return [AdjustmentEvent(
            symbol, TARGET, Decimal("1"), Decimal("1.052632"),
            source="akshare_sina_factor_multiplicative",
        )]


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


class FakeBaoStockVerification(FakeSecondary):
    def __init__(self, *, close: Decimal = Decimal("10.00")) -> None:
        super().__init__()
        self.close = close

    def bars_from_status(self, symbol: str, rows):
        bar = raw_bar(symbol, source="baostock")
        if self.close != bar.close:
            bar = DailyBar(
                source=bar.source, symbol=bar.symbol, exchange=bar.exchange,
                business_date=bar.business_date, open=self.close, high=self.close,
                low=self.close, close=self.close, previous_close=bar.previous_close,
                volume_shares=bar.volume_shares, amount_cny=bar.amount_cny,
                turnover_percent=bar.turnover_percent, trade_status=bar.trade_status,
                is_st=bar.is_st,
            )
        return {TARGET: bar}


class DailyCaptureTests(unittest.TestCase):
    def test_601866_false_continuity_is_overridden_by_candidate_inventory(self) -> None:
        symbol = "601866"
        candidate_plan = DailyIncrementalPlan(
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
        primary_bar = DailyBar(
            source="akshare_eastmoney", symbol=symbol, exchange="SSE", business_date=TARGET,
            open=Decimal("2.46"), high=Decimal("2.47"), low=Decimal("2.43"),
            close=Decimal("2.47"), previous_close=None, volume_shares=10000,
            amount_cny=Decimal("24600"), turnover_percent=Decimal("0.1"),
            trade_status="trading", is_st=None,
        )
        event = AdjustmentEvent(
            symbol, TARGET, Decimal("1.000000"), Decimal("1.236110"),
            source="akshare_sina_factor_multiplicative",
        )

        class EastmoneyPrimary(FakePrimary):
            def fetch_daily_raw_with_reference(self, _symbol: str, previous: date, target: date):
                return (
                    {TARGET: primary_bar}, "akshare_eastmoney", Decimal("2.4700"),
                    "akshare_eastmoney_change_amount",
                )

            def fetch_sina_adjustments(self, _symbol: str, end: date):
                return [event]

        class Verification(FakeVerification):
            def fetch_raw(self, _symbol: str, start: date, end: date, *, exclude_sources=None):
                return [replace(primary_bar, source="akshare_sina")]

        details = {
            "previous_session": PREVIOUS.isoformat(),
            "registration_date": PREVIOUS.isoformat(),
            "ex_rights_date": TARGET.isoformat(),
            "accepted_previous_close": "2.4700",
            "cash_per_ten_shares": "0.150000",
            "factor_reference_close": "2.455000",
            "derived_previous_close": "2.4600",
            "action_content": "10派0.15元",
            "vendor_action_sha256": "a" * 64,
        }
        with patch.object(
            TencentHistorySource,
            "fetch_cash_dividend_reference",
            return_value=(Decimal("2.4600"), details),
        ):
            evidence, reported, status, error = capture_symbol(
                plan=candidate_plan, symbol=symbol, primary_source=EastmoneyPrimary(event=event),
                verification_source=Verification(), secondary_source=None,
                fallback_suspended_symbols=frozenset(), fallback_status_available=True,
                previous_states={
                    symbol: PreviousAdjustedState(
                        symbol=symbol, business_date=PREVIOUS, raw_close=Decimal("2.4700"),
                        qfq_factor=Decimal("1.000000"), hfq_factor=Decimal("1.227273"),
                        source_dataset_id="accepted-predecessor",
                    )
                },
                ipo_dates={symbol: date(2007, 12, 26)},
                calendar_dates=(PREVIOUS, TARGET),
                corporate_action_record=corporate_action_record(
                    symbol, cash_per_ten="0.150000",
                ),
            )
        self.assertEqual((reported, status, error), (Decimal("2.4600"), "succeeded", None))
        self.assertEqual(evidence.adjusted_bars[0]["previous_close"], "2.4600")
        self.assertEqual(evidence.adjusted_bars[0]["hfq_factor"], "1.236110")
        self.assertEqual(
            evidence.lineage_evidence[0]["details"]["factor_reference_close"],
            "2.455000",
        )

    def test_eastmoney_candidate_forces_factor_and_structured_action_checks(self) -> None:
        candidate_plan = replace(
            plan(),
            corporate_action_inventory_count=1,
            corporate_action_inventory_sha256="f" * 64,
            corporate_action_symbols=("000001",),
        )
        event = AdjustmentEvent(
            "000001", TARGET, Decimal("1"), Decimal("1.052632"),
            source="akshare_sina_factor_multiplicative",
        )
        details = {
            "previous_session": PREVIOUS.isoformat(),
            "registration_date": PREVIOUS.isoformat(),
            "ex_rights_date": TARGET.isoformat(),
            "accepted_previous_close": "10.0000",
            "cash_per_ten_shares": "5.000000",
            "factor_reference_close": "9.500000",
            "derived_previous_close": "9.5000",
            "action_content": "10派5元",
            "vendor_action_sha256": "a" * 64,
        }
        with patch.object(
            TencentHistorySource,
            "fetch_cash_dividend_reference",
            return_value=(Decimal("9.5000"), details),
        ) as structured_action:
            evidence, reported, status, error = capture_symbol(
                plan=candidate_plan, symbol="000001", primary_source=FakePrimary(event=event),
                verification_source=FakeVerification(), secondary_source=None,
                fallback_suspended_symbols=frozenset(), fallback_status_available=True,
                previous_states={"000001": previous_state()},
                ipo_dates={"000001": date(1991, 4, 3)},
                calendar_dates=(PREVIOUS, TARGET),
                corporate_action_record=corporate_action_record(),
            )
        self.assertEqual((reported, status, error), (Decimal("9.5000"), "succeeded", None))
        structured_action.assert_called_once()
        self.assertEqual(evidence.adjusted_bars[0]["hfq_factor"], "1.052632")
        self.assertEqual(evidence.lineage_evidence[0]["kind"], "cash_dividend_reference")

        blocked, _reported, status, error = capture_symbol(
            plan=candidate_plan, symbol="000001", primary_source=FakePrimary(),
            verification_source=FakeVerification(), secondary_source=None,
            fallback_suspended_symbols=frozenset(), fallback_status_available=True,
            previous_states={"000001": previous_state()},
            ipo_dates={"000001": date(1991, 4, 3)},
            calendar_dates=(PREVIOUS, TARGET),
            corporate_action_record=corporate_action_record(),
        )
        self.assertEqual(status, "blocked")
        self.assertIn("no target factor event", str(error))
        self.assertEqual(len(blocked.primary_bars), 1)
        self.assertNotIn("missing_primary_bar", blocked.tradeability[0]["block_reasons"])
        self.assertEqual(blocked.adjusted_bars, [])

    def test_candidate_missing_sina_factors_uses_verified_pure_cash_deferred_event(self) -> None:
        candidate_plan = replace(
            plan(),
            corporate_action_inventory_count=1,
            corporate_action_inventory_sha256="f" * 64,
            corporate_action_symbols=("000001",),
        )
        details = {
            "previous_session": PREVIOUS.isoformat(),
            "registration_date": PREVIOUS.isoformat(),
            "ex_rights_date": TARGET.isoformat(),
            "accepted_previous_close": "10",
            "cash_per_ten_shares": "5.000000",
            "factor_reference_close": "9.500000",
            "derived_previous_close": "9.5000",
            "action_content": "10派5元",
            "vendor_action_sha256": "a" * 64,
        }
        capture_kwargs = {
            "plan": candidate_plan,
            "symbol": "000001",
            "primary_source": FakeSinaPrimary(),
            "verification_source": FakeVerification(),
            "secondary_source": None,
            "fallback_suspended_symbols": frozenset(),
            "fallback_status_available": True,
            "previous_states": {"000001": previous_state()},
            "ipo_dates": {"000001": date(1991, 4, 3)},
            "calendar_dates": (PREVIOUS, TARGET),
            "corporate_action_record": corporate_action_record(),
        }
        with patch.object(
            TencentHistorySource,
            "fetch_cash_dividend_reference",
            return_value=(Decimal("9.5000"), details),
        ):
            first = capture_symbol(**capture_kwargs)
            second = capture_symbol(**capture_kwargs)

        evidence, reported, status, error = first
        self.assertEqual((reported, status, error), (Decimal("9.5000"), "succeeded", None))
        self.assertEqual(first, second)
        self.assertEqual(
            evidence.adjusted_bars[0]["factor_source"],
            "rqalpha_deferred_cash_action",
        )
        self.assertEqual(evidence.adjusted_bars[0]["qfq_factor"], "1")
        self.assertEqual(evidence.adjusted_bars[0]["hfq_factor"], "1")
        self.assertEqual(
            evidence.adjustments[0]["source"],
            "rqalpha_deferred_cash_action",
        )
        lineage = evidence.lineage_evidence[0]
        self.assertEqual(lineage["kind"], "cash_dividend_reference")
        self.assertEqual(
            lineage["details"]["eastmoney_inventory_record"]["symbol"],
            "000001",
        )
        self.assertEqual(
            lineage["details"]["eastmoney_inventory_record_sha256"],
            sha256(lineage["details"]["eastmoney_inventory_record"]),
        )

    def test_candidate_missing_factors_blocks_conflicting_or_unsupported_actions(self) -> None:
        candidate_plan = replace(
            plan(),
            corporate_action_inventory_count=1,
            corporate_action_inventory_sha256="f" * 64,
            corporate_action_symbols=("000001",),
        )
        base_details = {
            "previous_session": PREVIOUS.isoformat(),
            "registration_date": PREVIOUS.isoformat(),
            "ex_rights_date": TARGET.isoformat(),
            "accepted_previous_close": "10",
            "cash_per_ten_shares": "4.000000",
            "factor_reference_close": "9.600000",
            "derived_previous_close": "9.6000",
            "action_content": "10派4元",
            "vendor_action_sha256": "a" * 64,
        }
        kwargs = {
            "plan": candidate_plan,
            "symbol": "000001",
            "primary_source": FakeSinaPrimary(),
            "verification_source": FakeVerification(),
            "secondary_source": None,
            "fallback_suspended_symbols": frozenset(),
            "fallback_status_available": True,
            "previous_states": {"000001": previous_state()},
            "ipo_dates": {"000001": date(1991, 4, 3)},
            "calendar_dates": (PREVIOUS, TARGET),
        }
        scenarios = (
            (corporate_action_record(), base_details, "cash dividend disagrees"),
            (
                corporate_action_record(),
                {**base_details, "cash_per_ten_shares": "5.000000", "registration_date": "2026-07-23"},
                "registration date does not match",
            ),
            (
                corporate_action_record(bonus_ratio="1.000000"),
                {**base_details, "cash_per_ten_shares": "5.000000"},
                "not pure cash",
            ),
            (
                corporate_action_record(symbol="600000"),
                {**base_details, "cash_per_ten_shares": "5.000000"},
                "symbol does not match",
            ),
            (
                {**corporate_action_record(), "ex_dividend_date": "2026-07-28"},
                {**base_details, "cash_per_ten_shares": "5.000000"},
                "ex-dividend date does not match",
            ),
            (
                {**corporate_action_record(), "equity_record_date": "2026-07-23"},
                {**base_details, "cash_per_ten_shares": "5.000000"},
                "equity-record date does not match",
            ),
            (
                {**corporate_action_record(), "cash_per_ten_shares": None},
                {**base_details, "cash_per_ten_shares": "5.000000"},
                "missing or nonpositive",
            ),
            (
                corporate_action_record(conversion_ratio="1.000000"),
                {**base_details, "cash_per_ten_shares": "5.000000"},
                "not pure cash",
            ),
        )
        for inventory_record, details, message in scenarios:
            with self.subTest(message=message), patch.object(
                TencentHistorySource,
                "fetch_cash_dividend_reference",
                return_value=(Decimal(str(details["derived_previous_close"])), details),
            ):
                evidence, _reported, status, error = capture_symbol(
                    **kwargs, corporate_action_record=inventory_record,
                )
            self.assertEqual(status, "blocked")
            self.assertIn(message, str(error))
            self.assertEqual(len(evidence.primary_bars), 1)
            self.assertEqual(len(evidence.tradeability), 1)
            self.assertEqual(evidence.adjusted_bars, [])
            self.assertFalse(evidence.tradeability[0]["can_buy"])
            self.assertFalse(evidence.tradeability[0]["can_sell"])
            self.assertIn(
                "invalid_adjustment_continuity",
                evidence.tradeability[0]["block_reasons"],
            )

    def test_baostock_is_last_resort_independent_verification_only(self) -> None:
        evidence, reported, status, error = capture_symbol(
            plan=plan(), symbol="000001", primary_source=FakePrimary(),
            verification_source=FakeVerification(fail=True), secondary_source=None,
            verification_fallback_source=FakeBaoStockVerification(),
            fallback_suspended_symbols=frozenset(), fallback_status_available=True,
            previous_states={"000001": previous_state()},
            ipo_dates={"000001": date(1991, 4, 3)},
            calendar_dates=(PREVIOUS, TARGET),
        )
        self.assertEqual((reported, status, error), (Decimal("10"), "succeeded", None))
        self.assertEqual(evidence.primary_bars[0]["source"], "akshare_eastmoney")
        self.assertEqual(evidence.verification_bars[0]["source"], "baostock")

        blocked, _reported, status, error = capture_symbol(
            plan=plan(), symbol="000001", primary_source=FakePrimary(),
            verification_source=FakeVerification(fail=True), secondary_source=None,
            verification_fallback_source=FakeBaoStockVerification(close=Decimal("20.00")),
            fallback_suspended_symbols=frozenset(), fallback_status_available=True,
            previous_states={"000001": previous_state()},
            ipo_dates={"000001": date(1991, 4, 3)},
            calendar_dates=(PREVIOUS, TARGET),
        )
        self.assertEqual(status, "blocked")
        self.assertIn("BaoStock verification disagrees", str(error))
        self.assertEqual(blocked.verification_bars, [])

    def test_baostock_blacklist_falls_back_without_opening_tradeability(self) -> None:
        evidence, reported, status, error = capture_symbol(
            plan=plan(), symbol="000001", primary_source=FakePrimary(),
            verification_source=FakeVerification(), secondary_source=None,
            fallback_suspended_symbols=frozenset(), fallback_status_available=True,
            previous_states={"000001": previous_state()},
            ipo_dates={"000001": date(1991, 4, 3)},
            calendar_dates=(PREVIOUS, TARGET),
        )
        self.assertEqual((reported, status, error), (Decimal("10"), "succeeded", None))
        self.assertEqual(evidence.manifest["status_source"], "akshare_eastmoney_dated_suspension")
        self.assertEqual(evidence.manifest["previous_close_source"], "akshare_eastmoney_change_amount")
        self.assertIsNone(evidence.tradeability[0]["is_st"])
        self.assertFalse(evidence.tradeability[0]["can_buy"])
        self.assertFalse(evidence.tradeability[0]["can_sell"])
        self.assertIn("unknown_st_status", evidence.tradeability[0]["block_reasons"])

    def test_eastmoney_confirmed_suspension_never_fabricates_a_bar(self) -> None:
        evidence, reported, status, error = capture_symbol(
            plan=plan(), symbol="000001", primary_source=FakePrimary(),
            verification_source=FakeVerification(), secondary_source=None,
            fallback_suspended_symbols=frozenset({"000001"}), fallback_status_available=True,
            previous_states={}, ipo_dates={"000001": date(1991, 4, 3)},
            calendar_dates=(PREVIOUS, TARGET),
        )
        self.assertEqual((reported, status, error), (None, "succeeded", None))
        self.assertEqual(evidence.primary_bars, [])
        self.assertTrue(evidence.tradeability[0]["is_suspended"])
        self.assertFalse(evidence.tradeability[0]["can_buy"])
        self.assertFalse(evidence.tradeability[0]["can_sell"])

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

    def test_sina_factor_gap_uses_independent_tencent_continuity_only(self) -> None:
        with patch.object(
            TencentHistorySource,
            "verify_no_adjustment_continuity",
            return_value="tencent_hfq_no_adjustment_continuity",
        ):
            evidence, reported, status, error = capture_symbol(
                plan=plan(), symbol="000001", primary_source=FakeSinaPrimary(),
                verification_source=FakeVerification(), secondary_source=None,
                fallback_suspended_symbols=frozenset(), fallback_status_available=True,
                previous_states={"000001": previous_state()},
                ipo_dates={"000001": date(1991, 4, 3)},
                calendar_dates=(PREVIOUS, TARGET),
            )
        self.assertEqual((reported, status, error), (Decimal("10"), "succeeded", None))
        self.assertIn(
            "tencent_hfq_no_adjustment_continuity",
            evidence.manifest["previous_close_source"],
        )

        class MalformedFactorPrimary(FakeSinaPrimary):
            def fetch_sina_adjustments(self, symbol: str, end: date):
                raise ValueError("malformed factor payload")

        malformed, _reported, status, error = capture_symbol(
            plan=plan(), symbol="000001", primary_source=MalformedFactorPrimary(),
            verification_source=FakeVerification(), secondary_source=None,
            fallback_suspended_symbols=frozenset(), fallback_status_available=True,
            previous_states={"000001": previous_state()},
            ipo_dates={"000001": date(1991, 4, 3)},
            calendar_dates=(PREVIOUS, TARGET),
        )
        self.assertEqual(status, "blocked")
        self.assertIn("malformed factor payload", str(error))
        self.assertEqual(len(malformed.primary_bars), 1)
        self.assertNotIn("missing_primary_bar", malformed.tradeability[0]["block_reasons"])
        self.assertEqual(malformed.adjusted_bars, [])

    def test_sina_ex_date_uses_tencent_cash_evidence_and_recomputes_limits(self) -> None:
        action_details = {
            "previous_session": PREVIOUS.isoformat(),
            "registration_date": PREVIOUS.isoformat(),
            "ex_rights_date": TARGET.isoformat(),
            "accepted_previous_close": "10",
            "cash_per_ten_shares": "5.000000",
            "derived_previous_close": "9.5000",
            "action_content": "10派5元",
            "vendor_action_sha256": "a" * 64,
        }
        with patch.object(
            TencentHistorySource,
            "fetch_cash_dividend_reference",
            return_value=(Decimal("9.5000"), action_details),
        ):
            evidence, reported, status, error = capture_symbol(
                plan=plan(), symbol="000001", primary_source=FakeDividendSinaPrimary(),
                verification_source=FakeVerification(), secondary_source=None,
                fallback_suspended_symbols=frozenset(), fallback_status_available=True,
                previous_states={"000001": previous_state()},
                ipo_dates={"000001": date(1991, 4, 3)},
                calendar_dates=(PREVIOUS, TARGET),
            )
        self.assertEqual((reported, status, error), (Decimal("9.5000"), "succeeded", None))
        self.assertEqual(evidence.manifest["previous_close_source"], "tencent_structured_cash_dividend")
        self.assertEqual(evidence.adjusted_bars[0]["previous_close"], "9.5000")
        self.assertEqual(evidence.lineage_evidence[0]["kind"], "cash_dividend_reference")
        self.assertIsNone(evidence.tradeability[0]["limit_up"])
        self.assertIn("unknown_st_status", evidence.tradeability[0]["block_reasons"])

    def test_cash_dividend_factor_reference_is_rebuilt_and_tamper_evident(self) -> None:
        eastmoney_record = corporate_action_record("601866", cash_per_ten="0.150000")
        details = {
            "previous_session": PREVIOUS.isoformat(),
            "registration_date": PREVIOUS.isoformat(),
            "ex_rights_date": TARGET.isoformat(),
            "accepted_previous_close": "2.4700",
            "cash_per_ten_shares": "0.150000",
            "factor_reference_close": "2.455000",
            "derived_previous_close": "2.4600",
            "action_content": "10派0.15元",
            "vendor_action_sha256": "a" * 64,
            "eastmoney_inventory_record": eastmoney_record,
            "eastmoney_inventory_record_sha256": sha256(eastmoney_record),
        }
        lineage = canonical_lineage_evidence({
            "symbol": "601866",
            "target_session": TARGET.isoformat(),
            "kind": "cash_dividend_reference",
            "source": "tencent_archive",
            "details": details,
        })
        self.assertEqual(
            _factor_reference_closes([lineage]),
            {"601866": Decimal("2.455000")},
        )

        tampered = {**lineage, "details": {**details, "factor_reference_close": "2.456000"}}
        with self.assertRaisesRegex(ValueError, "does not reconcile"):
            _factor_reference_closes([tampered])

        tampered_eastmoney = {
            **lineage,
            "details": {
                **details,
                "eastmoney_inventory_record": {
                    **eastmoney_record,
                    "cash_per_ten_shares": "0.160000",
                },
            },
        }
        with self.assertRaisesRegex(ValueError, "Eastmoney evidence hash"):
            canonical_lineage_evidence(tampered_eastmoney)

    def test_missing_exact_predecessor_is_recovered_only_with_gap_evidence(self) -> None:
        prior = date(2026, 7, 23)
        fallback_state = PreviousAdjustedState(
            symbol="000001", business_date=prior, raw_close=Decimal("9.8"),
            qfq_factor=Decimal("1"), hfq_factor=Decimal("2"),
            source_dataset_id="base-history",
        )
        recovery_details = {
            "prior_session": prior.isoformat(),
            "recovered_session": PREVIOUS.isoformat(),
            "accepted_prior_close": "9.8",
            "recovered_raw_close": "10.0000",
            "observed_sessions": [prior.isoformat(), PREVIOUS.isoformat()],
            "maximum_implied_hfq_change_rate": "0.001",
            "raw_rows_sha256": "b" * 64,
            "hfq_rows_sha256": "c" * 64,
        }
        with (
            patch.object(
                TencentHistorySource,
                "recover_no_adjustment_predecessor",
                return_value=(Decimal("10.0000"), recovery_details),
            ),
            patch.object(
                TencentHistorySource,
                "verify_no_adjustment_continuity",
                return_value="tencent_hfq_no_adjustment_continuity",
            ),
        ):
            exact_states: dict[str, PreviousAdjustedState] = {}
            evidence, reported, status, error = capture_symbol(
                plan=plan(), symbol="000001", primary_source=FakeSinaPrimary(),
                verification_source=FakeVerification(), secondary_source=None,
                fallback_suspended_symbols=frozenset(), fallback_status_available=True,
                previous_states=exact_states,
                fallback_previous_states={"000001": fallback_state},
                ipo_dates={"000001": date(1991, 4, 3)},
                calendar_dates=(prior, PREVIOUS, TARGET),
            )
        self.assertEqual((reported, status, error), (Decimal("10"), "succeeded", None))
        self.assertEqual(exact_states["000001"].business_date, PREVIOUS)
        self.assertEqual(evidence.lineage_evidence[0]["kind"], "gap_no_adjustment_recovery")
        self.assertEqual(evidence.adjusted_bars[0]["hfq_factor"], "2")

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
        self.assertEqual(len(missing.primary_bars), 1)
        self.assertNotIn("missing_primary_bar", missing.tradeability[0]["block_reasons"])
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

from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from urllib.parse import unquote

from scripts.simulation.contracts import simulation_identity
from scripts.simulation.ledger_acceptance import LEDGER_TABLES
from scripts.simulation.ledger_continuity_acceptance import (
    assert_continuity,
    build_continuity_packages,
    run_continuity_acceptance,
)


INPUT_SHA256 = "a29a5ef02cd2c1a67c9f08a40baa8ac61f1f0717dee8d03cd1f2a59a6841e185"
SOURCE_COMMIT = "a" * 40


def transaction_evidence() -> dict:
    return {
        "business_date": "2026-07-27",
        "trade": {
            "order_book_id": "600519.XSHG",
            "symbol": "600519",
            "side": "BUY",
            "exec_id": 17872161500000,
            "tax": "0.0000",
            "commission": "103.1600",
            "last_quantity": 100,
            "last_price": "1289.5000",
            "order_id": 17872161500000,
            "transaction_cost": "103.1600",
        },
        "portfolio": {
            "cash": "870946.8400",
            "total_value": "999896.8400",
            "market_value": "128950.0000",
        },
        "position": {
            "order_book_id": "600519.XSHG",
            "symbol": "600519",
            "quantity": 100,
            "last_price": "1289.5000",
            "avg_price": "1289.5000",
            "market_value": "128950.0000",
        },
        "account": {
            "cash": "870946.8400",
            "transaction_cost": "103.1600",
            "market_value": "128950.0000",
            "total_value": "999896.8400",
        },
    }


def holding_evidence() -> dict:
    return {
        "business_date": "2026-07-28",
        "portfolio": {
            "cash": "870946.8400",
            "total_value": "1000946.8400",
            "market_value": "130000.0000",
        },
        "account": {
            "cash": "870946.8400",
            "transaction_cost": "0.0000",
            "market_value": "130000.0000",
            "total_value": "1000946.8400",
        },
        "position": {
            "order_book_id": "600519.XSHG",
            "quantity": 100,
            "last_price": "1300.0000",
            "avg_price": "1289.5000",
            "market_value": "130000.0000",
        },
        "trace": {
            "business_date": "2026-07-28",
            "symbol": "600519",
            "total_shares": 100,
            "sellable_shares": 100,
            "average_execution_price": "1289.5000",
        },
        "transaction_trace": {
            "business_date": "2026-07-27",
            "symbol": "600519",
            "total_shares": 100,
            "sellable_shares": 0,
            "average_execution_price": "1289.5000",
        },
        "bar": {"symbol": "600519", "business_date": "2026-07-28", "adjustment": "none", "close": "1300.0000"},
        "tradeability": {"has_primary_bar": True, "can_buy": False, "can_sell": False},
    }


def packages():
    return build_continuity_packages(
        transaction_evidence(),
        holding_evidence(),
        input_sha256=INPUT_SHA256,
        source_commit=SOURCE_COMMIT,
    )


class FakeLedgerClient:
    def __init__(self, *, public: bool = False) -> None:
        self.public = public
        self.payloads: dict[str, dict] = {}
        self.fresh_publications = 0
        self.publication_calls = 0

    def rpc(self, name, payload):
        if self.public:
            raise RuntimeError("Supabase 401: permission denied")
        if name != "publish_v2_simulation_run":
            raise AssertionError(name)
        self.publication_calls += 1
        run_id = payload["p_manifest"]["run_id"]
        if run_id not in self.payloads:
            self.payloads[run_id] = copy.deepcopy(payload)
            self.fresh_publications += 1
            replay = False
        elif self.payloads[run_id] != payload:
            raise RuntimeError("Supabase 400: idempotency content mismatch")
        else:
            replay = True
        return {"run_id": run_id, "published": True, "idempotent_replay": replay}

    def rows(self, table, query):
        if self.public:
            raise RuntimeError("Supabase 401: permission denied")
        marker = "run_id=eq."
        if marker not in query:
            return []
        run_id = unquote(query.split(marker, 1)[1].split("&", 1)[0])
        payload = self.payloads.get(run_id)
        if payload is None:
            return []
        if table == "v2_simulation_runs":
            manifest = payload["p_manifest"]
            return [{
                "run_id": run_id,
                "idempotency_key": manifest["idempotency_key"],
                "predecessor_run_id": manifest["predecessor_run_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "payload_sha256": "1" * 64,
                "manifest_json": manifest,
                "simulation_only": True,
                "activation_state": "disabled_acceptance",
                "authoritative_account_write": False,
            }]
        for _, component_table, payload_key, _, _ in LEDGER_TABLES:
            if table == component_table:
                return copy.deepcopy(payload[payload_key])
        raise AssertionError(table)

    def request(self, method, path, body=None):
        if self.public:
            raise RuntimeError("Supabase 401: permission denied")
        if method in {"PATCH", "DELETE"}:
            raise RuntimeError("Supabase 403: immutable ledger")
        raise AssertionError((method, path, body))


class ContinuityPackageTests(unittest.TestCase):
    def test_no_trade_holding_day_carries_and_evaluates_position(self) -> None:
        first, second = packages()

        self.assertEqual(second.predecessor_run_id, first.run_id)
        self.assertEqual(second.snapshot.opening_cash, first.snapshot.cash)
        self.assertEqual(second.opening_positions[0].average_cost, first.closing_positions[0].average_cost)
        self.assertEqual(first.closing_positions[0].sellable_shares, 0)
        self.assertEqual(second.closing_positions[0].sellable_shares, 100)
        self.assertEqual(second.instructions, ())
        self.assertEqual(second.orders, ())
        self.assertEqual(second.fills, ())
        self.assertEqual(len(second.evaluations), 1)

    def test_missing_or_conflicting_holding_mark_fails_closed(self) -> None:
        evidence = holding_evidence()
        evidence["bar"]["close"] = "1299.0000"
        with self.assertRaisesRegex(ValueError, "mark differs"):
            build_continuity_packages(
                transaction_evidence(), evidence,
                input_sha256=INPUT_SHA256, source_commit=SOURCE_COMMIT,
            )

    def test_t_plus_one_state_must_come_from_rqalpha(self) -> None:
        evidence = holding_evidence()
        evidence["trace"]["sellable_shares"] = 0
        with self.assertRaisesRegex(ValueError, r"T\+1 state"):
            build_continuity_packages(
                transaction_evidence(), evidence,
                input_sha256=INPUT_SHA256, source_commit=SOURCE_COMMIT,
            )

    def test_wrong_predecessor_is_rejected(self) -> None:
        first, second = packages()
        run_id, key = simulation_identity(
            second.environment,
            second.business_date,
            second.data_release_id,
            second.strategy_version,
            second.source_commit,
            "wrong-predecessor",
        )
        wrong = replace(
            second,
            predecessor_run_id="wrong-predecessor",
            run_id=run_id,
            idempotency_key=key,
        )
        with self.assertRaisesRegex(ValueError, "predecessor_run_id"):
            assert_continuity(first, wrong)


class ContinuityOnlineAcceptanceTests(unittest.TestCase):
    def test_two_days_retry_idempotently_and_read_back_exactly(self) -> None:
        first, second = packages()
        service = FakeLedgerClient()
        report = run_continuity_acceptance(service, FakeLedgerClient(public=True), first, second)

        self.assertTrue(report["accepted"])
        self.assertEqual(service.fresh_publications, 2)
        self.assertEqual(service.publication_calls, 22)
        self.assertEqual(len(service.payloads), 2)
        self.assertEqual(report["holding_day"]["database_run_rows"], 1)
        self.assertEqual(report["holding_day"]["component_readback"]["instructions"]["count"], 0)
        self.assertEqual(report["holding_day"]["component_readback"]["positions"]["count"], 1)
        self.assertEqual(set(report["holding_day"]["reconciliation"]["differences"].values()), {"0.0000"})
        self.assertEqual(report["continuity"]["sellable_shares_t0"], 0)
        self.assertEqual(report["continuity"]["sellable_shares_t1"], 100)


if __name__ == "__main__":
    unittest.main()

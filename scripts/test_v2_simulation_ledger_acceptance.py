from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.simulation.ledger_acceptance import (
    LEDGER_TABLES,
    build_disabled_package,
    run_online_acceptance,
)
from scripts.simulation.reconciliation import reconcile
from scripts.simulation.run_store import publication_payload


INPUT_SHA256 = "a29a5ef02cd2c1a67c9f08a40baa8ac61f1f0717dee8d03cd1f2a59a6841e185"


def evidence() -> dict:
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


class FakeLedgerClient:
    def __init__(self, *, public: bool = False) -> None:
        self.public = public
        self.payload = None
        self.fresh_publications = 0
        self.publication_calls = 0

    def rpc(self, name, payload):
        if self.public:
            raise RuntimeError("Supabase 401: permission denied")
        self.publication_calls += 1
        if name != "publish_v2_simulation_run":
            raise AssertionError(name)
        if self.payload is None:
            self.payload = copy.deepcopy(payload)
            self.fresh_publications += 1
            replay = False
        elif payload != self.payload:
            raise RuntimeError("Supabase 400: idempotency content mismatch")
        else:
            replay = True
        return {
            "run_id": self.payload["p_manifest"]["run_id"],
            "published": True,
            "idempotent_replay": replay,
        }

    def rows(self, table, query):
        if self.public:
            raise RuntimeError("Supabase 401: permission denied")
        if self.payload is None:
            return []
        if table == "v2_simulation_runs":
            manifest = self.payload["p_manifest"]
            return [{
                "run_id": manifest["run_id"],
                "idempotency_key": manifest["idempotency_key"],
                "manifest_sha256": manifest["manifest_sha256"],
                "payload_sha256": "1" * 64,
                "manifest_json": manifest,
                "simulation_only": True,
                "activation_state": "disabled_acceptance",
                "authoritative_account_write": False,
            }]
        payload_key = next(item[2] for item in LEDGER_TABLES if item[1] == table)
        return copy.deepcopy(self.payload[payload_key])

    def request(self, method, path, body=None):
        if self.public:
            raise RuntimeError("Supabase 401: permission denied")
        raise RuntimeError("Supabase 400: V2 simulation ledger is append-only")


class DisabledLedgerPackageTest(unittest.TestCase):
    def test_builds_exact_fee_capitalized_transaction_day_package(self) -> None:
        package = build_disabled_package(
            evidence(), input_sha256=INPUT_SHA256, source_commit="commit-m34",
        )
        self.assertEqual(package.environment, "development")
        self.assertEqual(package.snapshot.cash, package.cash_entries[0].balance_after)
        self.assertEqual(str(package.closing_positions[0].average_cost), "1290.5316")
        self.assertEqual(str(package.closing_positions[0].floating_pnl), "-103.1600")
        self.assertEqual(package.closing_positions[0].sellable_shares, 0)
        self.assertEqual(set(reconcile(package)["differences"].values()), {"0.0000"})
        self.assertIn(INPUT_SHA256, package.data_release_id)

    def test_invalid_engine_cost_fails_closed(self) -> None:
        changed = evidence()
        changed["account"]["transaction_cost"] = "0.0000"
        with self.assertRaisesRegex(ValueError, "transaction cost"):
            build_disabled_package(
                changed, input_sha256=INPUT_SHA256, source_commit="commit-m34",
            )

    def test_wrong_date_or_input_hash_cannot_publish(self) -> None:
        changed = evidence()
        changed["business_date"] = "2026-07-28"
        with self.assertRaisesRegex(ValueError, "transaction date"):
            build_disabled_package(
                changed, input_sha256=INPUT_SHA256, source_commit="commit-m34",
            )
        with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
            build_disabled_package(evidence(), input_sha256="bad", source_commit="commit-m34")

    def test_publication_payload_remains_disabled_and_non_authoritative(self) -> None:
        package = build_disabled_package(
            evidence(), input_sha256=INPUT_SHA256, source_commit="commit-m34",
        )
        manifest = publication_payload(package)["p_manifest"]
        self.assertTrue(manifest["simulation_only"])
        self.assertFalse(manifest["authoritative_account_write"])
        self.assertEqual(manifest["activation_state"], "disabled_acceptance")


class OnlineAcceptanceTest(unittest.TestCase):
    def test_ten_retries_read_back_all_components_and_reject_mutation(self) -> None:
        package = build_disabled_package(
            evidence(), input_sha256=INPUT_SHA256, source_commit="commit-m34",
        )
        service = FakeLedgerClient()
        report = run_online_acceptance(service, FakeLedgerClient(public=True), package)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["publication_attempts"], 10)
        self.assertEqual(report["database_run_rows"], 1)
        self.assertEqual(service.fresh_publications, 1)
        self.assertEqual(service.publication_calls, 11)  # ten retries plus one tamper probe
        self.assertEqual(set(report["rejection_probes"].values()), {"rejected"})
        self.assertEqual(report["component_readback"]["fills"]["count"], 1)
        self.assertEqual(report["component_readback"]["opening_positions"]["count"], 0)


if __name__ == "__main__":
    unittest.main()

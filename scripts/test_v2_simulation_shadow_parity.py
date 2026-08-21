from __future__ import annotations

import unittest
from dataclasses import replace

from scripts.simulation.ledger_continuity_acceptance import run_continuity_acceptance
from scripts.simulation.shadow_parity_acceptance import (
    assert_execution_parity,
    assert_failure_parity,
    economic_sha256,
    to_disabled_shadow_packages,
)
from scripts.test_v2_simulation_ledger_continuity import FakeLedgerClient, packages


def normalized(result_sha256: str = "1" * 64) -> dict:
    return {
        "accepted": True,
        "authoritative": False,
        "simulation_orders_allowed": False,
        "result_sha256": result_sha256,
    }


def trace():
    return (
        {"business_date": "2026-07-27", "symbol": "600519", "total_shares": 100, "sellable_shares": 0},
        {"business_date": "2026-07-28", "symbol": "600519", "total_shares": 100, "sellable_shares": 100},
    )


class ShadowParityTests(unittest.TestCase):
    def test_shadow_identity_changes_no_economic_field(self) -> None:
        development = packages()
        shadow = to_disabled_shadow_packages(development)

        self.assertEqual([row.environment for row in shadow], ["shadow", "shadow"])
        self.assertNotEqual(shadow[0].run_id, development[0].run_id)
        self.assertEqual(shadow[1].predecessor_run_id, shadow[0].run_id)
        self.assertEqual(economic_sha256(shadow[0]), economic_sha256(development[0]))
        self.assertEqual(economic_sha256(shadow[1]), economic_sha256(development[1]))

    def test_two_independent_results_and_ledgers_match_exactly(self) -> None:
        reference = packages()
        rehearsal = packages()
        changed_order = replace(rehearsal[0].orders[0], order_id="rqalpha-order-independent-run")
        changed_fill = replace(
            rehearsal[0].fills[0],
            fill_id="rqalpha-fill-independent-run",
            order_id=changed_order.order_id,
        )
        changed_cash = replace(
            rehearsal[0].cash_entries[0],
            entry_id="cash-rqalpha-fill-independent-run",
            fill_id=changed_fill.fill_id,
        )
        rehearsal = (
            replace(
                rehearsal[0],
                orders=(changed_order,),
                fills=(changed_fill,),
                cash_entries=(changed_cash,),
            ),
            rehearsal[1],
        )
        report = assert_execution_parity(
            normalized(), trace(), reference,
            normalized(), trace(), rehearsal,
        )

        self.assertEqual(report["normalized_result_sha256"], "1" * 64)
        self.assertEqual(len(report["daily_economic_parity"]), 2)
        for day in report["daily_economic_parity"]:
            self.assertEqual(set(day["reconciliation"]["differences"].values()), {"0.0000"})

    def test_result_trace_or_ledger_difference_is_rejected(self) -> None:
        reference = packages()
        changed_evaluation = replace(
            reference[1].evaluations[0],
            blocked_reason="unexpected-shadow-difference",
        )
        changed_second = replace(reference[1], evaluations=(changed_evaluation,))

        with self.assertRaisesRegex(ValueError, "normalized results differ"):
            assert_execution_parity(
                normalized(), trace(), reference,
                normalized("2" * 64), trace(), reference,
            )
        with self.assertRaisesRegex(ValueError, "daily traces differ"):
            assert_execution_parity(
                normalized(), trace(), reference,
                normalized(), trace()[:-1], reference,
            )
        with self.assertRaisesRegex(ValueError, "economic ledger components differ"):
            assert_execution_parity(
                normalized(), trace(), reference,
                normalized(), trace(), (reference[0], changed_second),
            )

    def test_failure_type_and_reason_must_match(self) -> None:
        def missing_bar() -> None:
            raise ValueError("bar scope is incomplete")

        self.assertEqual(
            assert_failure_parity(missing_bar, missing_bar),
            {"type": "ValueError", "message": "bar scope is incomplete"},
        )
        with self.assertRaisesRegex(ValueError, "fail-closed behavior differs"):
            assert_failure_parity(
                missing_bar,
                lambda: (_ for _ in ()).throw(RuntimeError("different failure")),
            )

    def test_disabled_shadow_chain_replays_online_without_activation(self) -> None:
        shadow = to_disabled_shadow_packages(packages())
        service = FakeLedgerClient()
        report = run_continuity_acceptance(
            service,
            FakeLedgerClient(public=True),
            *shadow,
        )

        self.assertTrue(report["accepted"])
        self.assertEqual(service.fresh_publications, 2)
        self.assertEqual(service.publication_calls, 22)
        self.assertEqual({payload["p_manifest"]["environment"] for payload in service.payloads.values()}, {"shadow"})
        self.assertEqual(
            {payload["p_manifest"]["activation_state"] for payload in service.payloads.values()},
            {"disabled_acceptance"},
        )
        self.assertEqual(
            {payload["p_manifest"]["authoritative_account_write"] for payload in service.payloads.values()},
            {False},
        )


if __name__ == "__main__":
    unittest.main()

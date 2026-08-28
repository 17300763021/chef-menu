from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.simulation.contracts import (
    ENGINE_NAME, ENGINE_VERSION, AccountSnapshot, CashEntry, ClosingPosition, DataState,
    EngineFill, EngineOrder, OrderInstruction, OrderStatus, PositionEvaluation, PriceType,
    Side, SimulationPackage,
    simulation_identity,
)
from scripts.simulation.reconciliation import reconcile
from scripts.simulation.run_store import SimulationRunStore, publication_payload


def package() -> SimulationPackage:
    instruction = OrderInstruction.from_mapping({
        "instruction_id": "instruction-1", "symbol": "000001", "side": "buy", "quantity": 100,
        "price_type": "limit", "limit_price": "10", "business_date": "2026-07-31",
        "valid_until": "2026-07-31", "strategy_version": "strategy-v1", "data_release_id": "m2-release-1",
    })
    order = EngineOrder(
        "order-1", instruction.instruction_id, "000001", Side.BUY, 100, 100,
        PriceType.LIMIT, Decimal("10.0000"), OrderStatus.FILLED,
    )
    fill = EngineFill(
        "fill-1", "order-1", "000001", Side.BUY, 100, Decimal("10.0000"),
        Decimal("5.0000"), Decimal("0"), Decimal("2.0000"), Decimal("0"),
    )
    position = ClosingPosition(
        "000001", 100, 0, Decimal("10.0500"), Decimal("10.0000"),
        Decimal("1000.0000"), Decimal("-5.0000"), DataState.FRESH,
    )
    business_date = date(2026, 7, 31)
    run_id, idempotency_key = simulation_identity(
        "development", business_date, "m2-release-1", "strategy-v1", "commit-1",
    )
    return SimulationPackage(
        run_id=run_id, idempotency_key=idempotency_key, environment="development",
        business_date=business_date, data_release_id="m2-release-1",
        strategy_version="strategy-v1", source_commit="commit-1",
        engine_name=ENGINE_NAME, engine_version=ENGINE_VERSION, predecessor_run_id=None,
        opening_positions=(),
        instructions=(instruction,), orders=(order,), fills=(fill,),
        cash_entries=(CashEntry("cash-1", "fill-1", 1, Decimal("-1005.0000"), Decimal("98995.0000")),),
        closing_positions=(position,),
        evaluations=(PositionEvaluation("000001", DataState.FRESH, True, ""),),
        snapshot=AccountSnapshot(
            initial_capital=Decimal("100000.0000"), opening_cash=Decimal("100000.0000"),
            opening_realized_pnl=Decimal("0"),
            cash=Decimal("98995.0000"), market_value=Decimal("1000.0000"),
            total_equity=Decimal("99995.0000"), realized_pnl=Decimal("0"),
            floating_pnl=Decimal("-5.0000"), total_fees=Decimal("5.0000"),
        ),
    )


class ReconciliationTest(unittest.TestCase):
    def test_complete_buy_reconciles_to_zero_and_does_not_double_count_slippage(self) -> None:
        result = reconcile(package())
        self.assertTrue(result["accepted"])
        self.assertEqual(set(result["differences"].values()), {"0.0000"})

    def test_cash_difference_fails_the_entire_package(self) -> None:
        item = package()
        broken = replace(item, snapshot=replace(item.snapshot, cash=Decimal("98996")))
        with self.assertRaisesRegex(ValueError, "cash reconciliation"):
            reconcile(broken)

    def test_cash_sequence_must_be_replayable(self) -> None:
        item = package()
        broken = replace(item.cash_entries[0], sequence_no=2)
        with self.assertRaisesRegex(ValueError, "sequence must be contiguous"):
            reconcile(replace(item, cash_entries=(broken,)))

    def test_order_overfill_and_partial_cash_state_fail(self) -> None:
        item = package()
        broken_order = replace(item.orders[0], requested_quantity=99)
        with self.assertRaisesRegex(ValueError, "overfilled"):
            reconcile(replace(item, orders=(broken_order,)))

    def test_order_cannot_change_structured_instruction(self) -> None:
        item = package()
        changed = replace(item.orders[0], symbol="600000")
        with self.assertRaisesRegex(ValueError, "differs from its structured instruction"):
            reconcile(replace(item, orders=(changed,)))

    def test_rejected_order_changes_no_cash_or_position(self) -> None:
        item = package()
        rejected = replace(
            item.orders[0], filled_quantity=0, status=OrderStatus.REJECTED,
            reject_reason="market_data_missing",
        )
        empty_snapshot = AccountSnapshot(
            initial_capital=Decimal("100000"), opening_cash=Decimal("100000"),
            opening_realized_pnl=Decimal("0"), cash=Decimal("100000"),
            market_value=Decimal("0"), total_equity=Decimal("100000"), realized_pnl=Decimal("0"),
            floating_pnl=Decimal("0"), total_fees=Decimal("0"),
        )
        result = reconcile(replace(
            item, orders=(rejected,), fills=(), cash_entries=(), closing_positions=(),
            evaluations=(), snapshot=empty_snapshot,
        ))
        self.assertTrue(result["accepted"])

    def test_buy_fill_cannot_claim_realized_pnl(self) -> None:
        item = package()
        invalid_fill = replace(item.fills[0], realized_pnl=Decimal("1"))
        invalid_snapshot = replace(
            item.snapshot, realized_pnl=Decimal("1"), total_equity=Decimal("99996"),
        )
        with self.assertRaisesRegex(ValueError, "cannot realize PnL"):
            reconcile(replace(item, fills=(invalid_fill,), snapshot=invalid_snapshot))

    def test_every_open_position_requires_exactly_one_evaluation(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one daily evaluation"):
            reconcile(replace(package(), evaluations=()))

    def test_missing_market_data_must_be_exposed_and_blocked(self) -> None:
        item = package()
        position = replace(item.closing_positions[0], data_state=DataState.MISSING)
        unblocked = replace(item.evaluations[0], data_state=DataState.MISSING, blocked_reason="")
        with self.assertRaisesRegex(ValueError, "not exposed and blocked"):
            reconcile(replace(item, closing_positions=(position,), evaluations=(unblocked,)))
        blocked = replace(unblocked, blocked_reason="market_data_missing")
        self.assertTrue(reconcile(replace(item, closing_positions=(position,), evaluations=(blocked,)))["accepted"])

    def test_content_hash_and_publication_payload_are_deterministic(self) -> None:
        self.assertEqual(publication_payload(package()), publication_payload(package()))
        self.assertEqual(len(publication_payload(package())["p_manifest"]["manifest_sha256"]), 64)


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        return {"run_id": payload["p_manifest"]["run_id"], "published": True, "idempotent_replay": False}


class RunStoreTest(unittest.TestCase):
    def test_store_uses_one_atomic_rpc(self) -> None:
        client = FakeClient()
        result = SimulationRunStore(client).publish(package())
        self.assertTrue(result["published"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "publish_v2_simulation_run")

    def test_ten_retries_share_one_business_identity(self) -> None:
        identities = {
            simulation_identity("shadow", date(2026, 7, 31), "release", "strategy", "commit")
            for _ in range(10)
        }
        self.assertEqual(len(identities), 1)
        first = simulation_identity("shadow", date(2026, 7, 31), "release", "strategy", "commit", "prior-a")
        changed = simulation_identity("shadow", date(2026, 7, 31), "release", "strategy", "commit", "prior-b")
        self.assertNotEqual(first, changed)


if __name__ == "__main__":
    unittest.main()

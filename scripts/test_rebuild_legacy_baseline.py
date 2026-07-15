from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rebuild_legacy_baseline import replay_account


def filled(order_id, side, shares, price, fee, before, after, cash_before="0", cash_after="0", slippage="0"):
    amount = str(shares * price)
    return {
        "id": order_id,
        "created_at": f"2026-01-0{1 if side == 'buy' else 2}T00:00:00+00:00",
        "code": "600001",
        "name": "测试股",
        "side": side,
        "price": str(price),
        "shares": shares,
        "amount": amount,
        "fee_amount": str(fee),
        "slippage_amount": str(slippage),
        "cash_before": cash_before,
        "cash_after": cash_after,
        "position_shares_before": before,
        "position_shares_after": after,
        "realized_pnl": "78" if side == "sell" else "0",
    }


class RebuildLegacyBaselineTest(unittest.TestCase):
    def test_buy_fee_sell_fee_and_partial_position_reconcile(self):
        orders = [
            filled("buy", "buy", 100, 10, 5, 0, 100, slippage="50"),
            filled("sell", "sell", 40, 12, 2, 100, 60, slippage="20"),
        ]
        references = [{"id": "position", "code": "600001", "name": "测试股", "shares": 60, "market_value": "660", "status": "open"}]
        account, entries, positions = replay_account("test", "测试", orders, references, {})
        self.assertEqual(account["cash"], "999473.00")
        self.assertEqual(account["realized_pnl"], "76.00")
        self.assertEqual(account["floating_pnl"], "57.00")
        self.assertEqual(account["total_pnl"], "133.00")
        self.assertEqual(account["total_assets"], "1000133.00")
        self.assertEqual(positions[0]["total_book_cost"], "603.00")
        self.assertEqual(account["recorded_slippage_total"], "70.00")
        self.assertEqual(entries[1]["reconstructed_realized_pnl"], "76.00")

    def test_slippage_is_not_deducted_twice(self):
        order = filled("buy", "buy", 100, 10, 0, 0, 100, slippage="999")
        references = [{"id": "position", "code": "600001", "shares": 100, "market_value": "1000", "status": "open"}]
        account, _, _ = replay_account("test", "测试", [order], references, {})
        self.assertEqual(account["total_assets"], "1000000.00")
        self.assertEqual(account["recorded_slippage_total"], "999.00")
        self.assertEqual(account["reconciliation"]["initial_plus_pnl_difference"], "0.00")

    def test_missing_frozen_valuation_fails_closed(self):
        order = filled("buy", "buy", 100, 10, 0, 0, 100)
        with self.assertRaisesRegex(RuntimeError, "冻结估值无效"):
            references = [{"id": "position", "code": "600001", "shares": 100, "market_value": None, "status": "open"}]
            replay_account("test", "测试", [order], references, {})

    def test_position_transition_mismatch_fails(self):
        order = filled("buy", "buy", 100, 10, 0, 10, 110)
        references = [{"id": "position", "code": "600001", "shares": 100, "market_value": "1000", "status": "open"}]
        with self.assertRaisesRegex(RuntimeError, "成交前持仓不连续"):
            replay_account("test", "测试", [order], references, {})

    def test_migration_is_atomic_private_and_append_only(self):
        migrations = sorted((ROOT / "supabase" / "migrations").glob("*_add_legacy_account_baselines.sql"))
        self.assertEqual(len(migrations), 1)
        sql = migrations[0].read_text(encoding="utf-8").lower()
        self.assertIn("publish_legacy_account_baseline", sql)
        self.assertIn("before update or delete or truncate", sql)
        self.assertIn("security invoker", sql)
        self.assertIn("revoke all on table public.legacy_account_baselines from anon, authenticated", sql)


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import model_trade_engine
import paper_trade_engine
import run_stock_tasks


class LegacyAccountFreezeTest(unittest.TestCase):
    def test_paper_engine_fails_closed_before_creating_client(self) -> None:
        original_get_client = paper_trade_engine.get_client
        paper_trade_engine.LEGACY_ACCOUNT_FROZEN = True
        paper_trade_engine.get_client = lambda: self.fail("frozen engine must not create a client")
        try:
            self.assertEqual(
                paper_trade_engine.run(dry_run=False),
                {"decisions": 0, "positions": 0, "orders": 0, "snapshots": 0},
            )
        finally:
            paper_trade_engine.get_client = original_get_client

    def test_model_engine_fails_closed_before_creating_client(self) -> None:
        original_get_client = model_trade_engine.get_client
        model_trade_engine.LEGACY_ACCOUNT_FROZEN = True
        model_trade_engine.get_client = lambda: self.fail("frozen engine must not create a client")
        try:
            self.assertEqual(
                model_trade_engine.run(dry_run=False),
                {"predictions": 0, "decisions": 0, "orders": 0, "snapshots": 0},
            )
        finally:
            model_trade_engine.get_client = original_get_client

    def test_task_runner_does_not_spawn_legacy_engine(self) -> None:
        original_run_command = run_stock_tasks.run_command
        run_stock_tasks.LEGACY_ACCOUNT_FROZEN = True
        run_stock_tasks.run_command = lambda *_args, **_kwargs: self.fail("frozen task must not spawn engine")
        try:
            self.assertFalse(run_stock_tasks.run_paper_trade())
        finally:
            run_stock_tasks.run_command = original_run_command

    def test_migration_covers_every_legacy_ledger(self) -> None:
        migrations = sorted((ROOT / "supabase" / "migrations").glob("*_freeze_legacy_stock_ledgers.sql"))
        self.assertEqual(len(migrations), 1)
        sql = migrations[0].read_text(encoding="utf-8")
        for table in (
            "stock_positions",
            "stock_trade_history",
            "stock_auto_trade_orders",
            "stock_portfolio_snapshots",
            "stock_model_positions",
            "stock_model_orders",
            "stock_model_trade_history",
            "stock_model_portfolio_snapshots",
        ):
            self.assertIn(f"'{table}'", sql)
        self.assertIn("before insert or update or delete or truncate", sql.lower())


if __name__ == "__main__":
    unittest.main()

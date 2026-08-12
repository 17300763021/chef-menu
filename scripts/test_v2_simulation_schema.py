from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SimulationSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = ROOT / "supabase" / "migrations" / "20260812090000_add_v2_simulation_ledger.sql"
        cls.sql = cls.path.read_text(encoding="utf-8").lower()

    def test_separate_immutable_ledger_tables_exist(self) -> None:
        for table in (
            "v2_simulation_runs", "v2_simulation_decisions", "v2_simulation_orders",
            "v2_simulation_fills", "v2_simulation_cash_entries", "v2_simulation_positions",
            "v2_simulation_position_evaluations", "v2_simulation_opening_positions",
        ):
            self.assertIn(f"create table public.{table}", self.sql)
            self.assertIn(f"alter table public.{table} enable row level security", self.sql)
            self.assertIn(f"revoke all on table public.{table} from public, anon, authenticated", self.sql)
            self.assertIn(f"{table}_append_only", self.sql)

    def test_publication_is_single_atomic_fail_closed_rpc(self) -> None:
        self.assertIn("create or replace function public.publish_v2_simulation_run", self.sql)
        self.assertIn("non-zero reconciliation difference cannot publish", self.sql)
        self.assertIn("open-position evaluation coverage is incomplete", self.sql)
        self.assertIn("on conflict (idempotency_key) do nothing", self.sql)
        self.assertIn("idempotent_replay", self.sql)
        self.assertIn("security definer", self.sql)
        self.assertIn("reconciliation inventory is incomplete", self.sql)
        self.assertIn("payload_sha256", self.sql)
        self.assertIn("extensions.digest", self.sql)
        self.assertIn("server-side account reconciliation failed", self.sql)
        self.assertIn("closing position quantities do not reconcile", self.sql)
        self.assertNotIn("dblink", self.sql)

    def test_m31_cannot_activate_main_or_real_trading(self) -> None:
        self.assertIn("activation_state = 'disabled_acceptance'", self.sql)
        self.assertIn("not authoritative_account_write", self.sql)
        self.assertIn("environment in ('development', 'shadow')", self.sql)
        self.assertNotIn("broker", self.sql)

    def test_frontend_has_no_ledger_write_grant(self) -> None:
        self.assertNotIn("grant select, insert on table public.v2_simulation", self.sql)
        self.assertNotIn("grant insert on table public.v2_simulation", self.sql)
        self.assertIn("grant execute on function public.publish_v2_simulation_run", self.sql)
        self.assertIn("to service_role", self.sql)


if __name__ == "__main__":
    unittest.main()

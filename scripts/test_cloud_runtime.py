from __future__ import annotations

import sys
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cloud_runtime import build_claim, complete_claim, current_half_hour_slot, idempotency_key


class FakeClient:
    def __init__(self, claim: dict) -> None:
        self.claim = claim
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name: str, payload: dict) -> dict:
        self.calls.append((name, payload))
        if name == "claim_cloud_job":
            return dict(self.claim)
        if name == "heartbeat_cloud_job":
            return {"status": "running"}
        if name == "finish_cloud_job":
            return {"status": "succeeded", "result_published": False}
        raise AssertionError(name)


class CloudRuntimeTest(unittest.TestCase):
    def test_idempotency_key_is_deterministic_and_version_aware(self) -> None:
        first = build_claim("shadow", "foundation_heartbeat", "slot", "commit-a")
        second = build_claim("shadow", "foundation_heartbeat", "slot", "commit-a", {"extra": 1})
        changed = build_claim("shadow", "foundation_heartbeat", "slot", "commit-b")
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertNotEqual(first["idempotency_key"], changed["idempotency_key"])

    def test_idempotency_key_requires_stable_business_fields(self) -> None:
        payload = {
            "environment": "shadow",
            "business_date": "2026-07-15",
            "job_type": "foundation_heartbeat",
            "run_slot": "slot",
            "source_commit": "commit",
        }
        original = idempotency_key(payload)
        payload["run_slot"] = "other"
        self.assertNotEqual(original, idempotency_key(payload))

    def test_half_hour_slot_is_utc_and_stable(self) -> None:
        now = datetime(2026, 7, 15, 9, 44, 59, tzinfo=timezone.utc)
        self.assertEqual(current_half_hour_slot(now), "20260715T0930Z")

    def test_complete_claim_heartbeats_then_finishes(self) -> None:
        client = FakeClient({"allowed": True, "status": "claimed", "run_id": "run-1"})
        result = complete_claim(client, build_claim("shadow", "foundation_heartbeat", "slot", "commit"))
        self.assertEqual([name for name, _ in client.calls], ["claim_cloud_job", "heartbeat_cloud_job", "finish_cloud_job"])
        self.assertEqual(result["finish"]["status"], "succeeded")

    def test_blocked_claim_never_heartbeats_or_finishes(self) -> None:
        client = FakeClient({"allowed": False, "status": "blocked", "run_id": "run-1", "quota_decision": "blocked_100"})
        result = complete_claim(client, build_claim("shadow", "foundation_heartbeat", "slot", "commit"))
        self.assertEqual([name for name, _ in client.calls], ["claim_cloud_job"])
        self.assertFalse(result["allowed"])

    def test_migration_is_private_atomic_and_cost_gated(self) -> None:
        migration = next((ROOT / "supabase" / "migrations").glob("*_add_cloud_runtime_foundation.sql"))
        sql = migration.read_text(encoding="utf-8").lower()
        self.assertIn("create extension if not exists pg_cron", sql)
        self.assertIn("create or replace function public.claim_cloud_job", sql)
        self.assertIn("on conflict (idempotency_key) do nothing", sql)
        self.assertIn("blocked_100", sql)
        self.assertIn("before update or delete or truncate", sql)
        self.assertNotIn("pg_net", sql)
        self.assertNotIn("vault.create_secret", sql)

    def test_legacy_automatic_schedules_are_disabled(self) -> None:
        for name in ("stock-tasks.yml", "stock-pending.yml"):
            workflow = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertNotIn("schedule:", workflow)
            self.assertIn("workflow_dispatch:", workflow)

    def test_cloud_workflow_is_bounded_and_actions_are_sha_pinned(self) -> None:
        workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
        cloud = (ROOT / ".github" / "workflows" / "cloud-runtime.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "17,47 * * * 1-5"', cloud)
        self.assertIn("timeout-minutes: 15", cloud)
        self.assertIn("cancel-in-progress: false", cloud)
        for path in workflows:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "uses: actions/" in line:
                    reference = line.split("@", 1)[1].split()[0]
                    self.assertRegex(reference, r"^[0-9a-f]{40}$", str(path))

    def test_frontend_cannot_embed_service_role(self) -> None:
        for path in (ROOT / "src").rglob("*"):
            if path.is_file() and path.suffix in {".ts", ".tsx", ".js", ".jsx", ".css", ".html"}:
                self.assertNotIn("SERVICE_ROLE", path.read_text(encoding="utf-8"), str(path))
        supabase_client = (ROOT / "src" / "lib" / "supabase.ts").read_text(encoding="utf-8")
        self.assertIn("VITE_SUPABASE_PUBLISHABLE_KEY", supabase_client)

    def test_javascript_direct_dependencies_are_exact(self) -> None:
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        for group in ("dependencies", "devDependencies"):
            for name, version in package[group].items():
                self.assertNotRegex(version, r"^[~^><=*]", name)
        self.assertEqual(package["engines"]["node"], (ROOT / ".nvmrc").read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture_legacy_evidence import canonical_json, current_account_snapshot, deterministic_zip, sha256


class CaptureLegacyEvidenceTest(unittest.TestCase):
    def test_canonical_json_and_hash_ignore_dictionary_key_order(self) -> None:
        first = canonical_json([{"b": 2, "a": 1}])
        second = canonical_json([{"a": 1, "b": 2}])
        self.assertEqual(first, second)
        self.assertEqual(sha256(first), sha256(second))

    def test_zip_is_deterministic_and_contains_no_local_timestamps(self) -> None:
        files = {"b.json": b"two", "a.json": b"one"}
        self.assertEqual(deterministic_zip(files), deterministic_zip(dict(reversed(list(files.items())))))

    def test_current_snapshot_uses_open_positions_and_latest_snapshot(self) -> None:
        exports = {
            "stock_positions": [{"id": 1, "status": "open"}, {"id": 2, "status": "closed"}],
            "stock_portfolio_snapshots": [{"snapshot_time": "2026-01-01"}, {"snapshot_time": "2026-01-02"}],
            "stock_model_positions": [{"id": 3, "status": "open"}],
            "stock_model_portfolio_snapshots": [{"snapshot_time": "2026-01-03"}],
        }
        snapshot = current_account_snapshot(exports)
        self.assertEqual([row["id"] for row in snapshot["legacy_open_positions"]], [1])
        self.assertEqual(snapshot["legacy_latest_snapshot"]["snapshot_time"], "2026-01-02")

    def test_migration_is_append_only_and_private(self) -> None:
        migrations = sorted((ROOT / "supabase" / "migrations").glob("*_add_legacy_evidence_manifests.sql"))
        self.assertEqual(len(migrations), 1)
        sql = migrations[0].read_text(encoding="utf-8").lower()
        self.assertIn("before update or delete or truncate", sql)
        self.assertIn("enable row level security", sql)
        self.assertIn("revoke all on table public.legacy_evidence_manifests from anon, authenticated", sql)
        self.assertIn("security invoker", sql)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.market_data.industry_quota_guard import evaluate_industry_quota


NOW = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)


class IndustryQuotaGuardTest(unittest.TestCase):
    def test_both_free_quotas_below_80_are_allowed(self) -> None:
        result = evaluate_industry_quota(
            ru_percent="27.66", storage_percent="73.2%", checked_at="2026-07-28", now=NOW,
        )
        self.assertTrue(result["allowed"])

    def test_ru_at_80_stops_nonessential_work(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "monthly RU"):
            evaluate_industry_quota(
                ru_percent="80", storage_percent="10", checked_at="2026-07-28", now=NOW,
            )

    def test_storage_at_80_stops_nonessential_work(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "row-storage"):
            evaluate_industry_quota(
                ru_percent="10", storage_percent="80", checked_at="2026-07-28", now=NOW,
            )

    def test_stale_attestation_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "8 days old"):
            evaluate_industry_quota(
                ru_percent="10", storage_percent="10", checked_at="2026-07-20", now=NOW,
            )

    def test_invalid_or_future_attestation_fails_closed(self) -> None:
        for ru, storage, checked_at in (
            ("bad", "10", "2026-07-28"),
            ("10", "101", "2026-07-28"),
            ("10", "10", "bad"),
        ):
            with self.assertRaises(RuntimeError):
                evaluate_industry_quota(
                    ru_percent=ru, storage_percent=storage, checked_at=checked_at, now=NOW,
                )
        with self.assertRaisesRegex(RuntimeError, "future"):
            evaluate_industry_quota(
                ru_percent="10", storage_percent="10", checked_at="2026-07-29", now=NOW,
            )

    def test_workflow_is_manual_bounded_resumable_and_node24_pinned(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "market-data-industry-acceptance.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("\n  schedule:", workflow)
        self.assertIn("\n  pull_request:", workflow)
        self.assertIn("resume_run_id:", workflow)
        self.assertIn("max-parallel: 4", workflow)
        self.assertIn("retention-days: 7", workflow)
        self.assertIn('FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"', workflow)
        self.assertIn("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", workflow)
        self.assertIn("actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1", workflow)
        self.assertIn("secrets.TIDB_PASSWORD", workflow)
        self.assertNotIn("TIDB_HOST: gateway", workflow)


if __name__ == "__main__":
    unittest.main()

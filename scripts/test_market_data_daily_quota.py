from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.market_data.daily_quota_guard import evaluate_daily_quota


NOW = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)


class DailyQuotaGuardTests(unittest.TestCase):
    def test_manual_run_below_threshold_with_fresh_attestation_passes(self) -> None:
        result = evaluate_daily_quota(
            event_name="workflow_dispatch", schedule_enabled="false",
            reported_percent="29.4", checked_at="2026-07-28", now=NOW,
            storage_percent="35.0",
        )
        self.assertTrue(result["allowed"])

    def test_schedule_requires_explicit_enablement(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            evaluate_daily_quota(
                event_name="schedule", schedule_enabled="false",
                reported_percent="10", checked_at="2026-07-28", now=NOW,
                storage_percent="10",
            )

    def test_eighty_percent_or_stale_attestation_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "80%"):
            evaluate_daily_quota(
                event_name="workflow_dispatch", schedule_enabled="false",
                reported_percent="80", checked_at="2026-07-28", now=NOW,
                storage_percent="10",
            )
        with self.assertRaisesRegex(RuntimeError, "days old"):
            evaluate_daily_quota(
                event_name="workflow_dispatch", schedule_enabled="false",
                reported_percent="10", checked_at="2026-07-20", now=NOW,
                storage_percent="10",
            )

    def test_missing_invalid_or_future_attestation_fails_closed(self) -> None:
        for percent, checked_at in (("", "2026-07-28"), ("101", "2026-07-28"), ("10", "bad")):
            with self.assertRaises(RuntimeError):
                evaluate_daily_quota(
                    event_name="workflow_dispatch", schedule_enabled="false",
                    reported_percent=percent, checked_at=checked_at, now=NOW,
                    storage_percent="10",
                )
        with self.assertRaisesRegex(RuntimeError, "future"):
            evaluate_daily_quota(
                event_name="workflow_dispatch", schedule_enabled="false",
                reported_percent="10", checked_at="2026-07-29", now=NOW,
                storage_percent="10",
            )

    def test_storage_at_eighty_percent_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "row-storage"):
            evaluate_daily_quota(
                event_name="workflow_dispatch", schedule_enabled="false",
                reported_percent="10", storage_percent="80",
                checked_at="2026-07-28", now=NOW,
            )

    def test_cloud_workflow_is_disabled_by_default_and_uses_compact_retention(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "market-data-daily-incremental.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("vars.M2_DAILY_ENABLED == 'true'", workflow)
        self.assertEqual(
            workflow.count(
                "github.event_name == 'workflow_dispatch' || "
                "(github.event_name == 'schedule' && vars.M2_DAILY_ENABLED == 'true')"
            ),
            2,
        )
        self.assertIn('if [[ "$GITHUB_EVENT_NAME" == "schedule" ]]', workflow)
        self.assertIn("max_sessions=5", workflow)
        self.assertIn("--reported-percent", workflow)
        self.assertIn("--storage-percent", workflow)
        self.assertIn("retention-days: 7", workflow)
        self.assertIn("daily-market-increment/session-*/manifest.json", workflow)
        self.assertNotIn("daily-market-increment/*", workflow)
        self.assertIn("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", workflow)
        self.assertIn("actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1", workflow)
        self.assertIn("actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f", workflow)


if __name__ == "__main__":
    unittest.main()

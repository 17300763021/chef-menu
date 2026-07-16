from pathlib import Path
from unittest.mock import patch
import unittest
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")


class FakeClient:
    def __init__(self, pending=None, latest_jobs=None):
        self.pending = pending or []
        self.latest_jobs = latest_jobs or {}
        self.inserts = []
        self.requests = []

    def request(self, method, path, body=None, prefer=None):
        self.requests.append((method, path, body, prefer))
        if path.startswith("stock_job_requests?status=eq.pending"):
            return self.pending
        if path.startswith("stock_job_runs?select=started_at"):
            for job_type, rows in self.latest_jobs.items():
                if job_type.replace(" ", "%20") in path or job_type in path:
                    return rows
            return []
        return []

    def insert(self, table, rows):
        self.inserts.append((table, rows))
        return len(rows)


class StockWorkflowScheduleTest(unittest.TestCase):
    def test_frozen_legacy_workflows_are_manual_only(self) -> None:
        stock_tasks = (ROOT / ".github" / "workflows" / "stock-tasks.yml").read_text(encoding="utf-8")
        pending_tasks = (ROOT / ".github" / "workflows" / "stock-pending.yml").read_text(encoding="utf-8")

        self.assertNotIn("schedule:", stock_tasks)
        self.assertNotIn("schedule:", pending_tasks)
        self.assertIn("workflow_dispatch:", stock_tasks)
        self.assertIn("workflow_dispatch:", pending_tasks)
        self.assertIn('python scripts/run_stock_tasks.py --mode pending', pending_tasks)

    def test_m2_source_acceptance_has_no_schedule_or_account_secret(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "market-data-source-acceptance.yml").read_text(encoding="utf-8")
        self.assertNotIn("schedule:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", workflow)

    def test_stock_jobs_use_shanghai_timezone(self) -> None:
        for workflow_name in ["stock-tasks.yml", "stock-pending.yml"]:
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
            self.assertIn("TZ: Asia/Shanghai", workflow)

    def test_night_scan_defaults_to_broad_after_hours_universe(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import run_stock_tasks

        with patch.dict(run_stock_tasks.os.environ, {}, clear=True), \
                patch.object(run_stock_tasks.subprocess, "run") as run:
            run_stock_tasks.run_night_scan()

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--limit") + 1], "1000")

    def test_pending_records_heartbeat_when_no_requests(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import run_stock_tasks

        client = FakeClient()
        with patch.object(run_stock_tasks, "get_client", return_value=client), \
                patch.object(run_stock_tasks, "run_stock_task_watchdog", return_value=None):
            run_stock_tasks.process_pending()

        self.assertIn(("stock_job_runs", [{
            "job_type": "GitHub Actions: pending",
            "status": "success",
            "imported_count": 0,
            "error_message": "No pending stock task requests.",
        }]), client.inserts)

    def test_watchdog_backfills_missing_intraday_stock_task_once(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import run_stock_tasks

        client = FakeClient()
        now = datetime(2026, 6, 29, 13, 53, tzinfo=SHANGHAI)
        with patch.object(run_stock_tasks, "execute_job", return_value=2) as execute:
            result = run_stock_tasks.run_stock_task_watchdog(client, now)

        execute.assert_called_once_with("live_decision")
        self.assertEqual(result, "live_decision")
        self.assertIn(("stock_job_runs", [{
            "job_type": "GitHub Actions watchdog: live_decision",
            "status": "success",
            "imported_count": 2,
            "error_message": "Backfilled missing scheduled stock task.",
        }]), client.inserts)


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StockWorkflowScheduleTest(unittest.TestCase):
    def test_live_and_pending_schedules_are_isolated(self) -> None:
        stock_tasks = (ROOT / ".github" / "workflows" / "stock-tasks.yml").read_text(encoding="utf-8")
        pending_tasks = (ROOT / ".github" / "workflows" / "stock-pending.yml").read_text(encoding="utf-8")

        self.assertNotIn('cron: "*/5 * * * 1-5"', stock_tasks)
        self.assertIn('cron: "*/5 * * * 1-5"', pending_tasks)
        self.assertIn('python scripts/run_stock_tasks.py --mode pending', pending_tasks)
        self.assertIn('cron: "40 7 * * 1-5"', stock_tasks)

    def test_live_schedule_matches_a_share_trading_hours(self) -> None:
        stock_tasks = (ROOT / ".github" / "workflows" / "stock-tasks.yml").read_text(encoding="utf-8")

        expected = [
            'cron: "30-55/5 1 * * 1-5"',
            'cron: "*/5 2 * * 1-5"',
            'cron: "0-30/5 3 * * 1-5"',
            'cron: "*/5 5-6 * * 1-5"',
            'cron: "0 7 * * 1-5"',
        ]
        for schedule in expected:
            self.assertIn(schedule, stock_tasks)

    def test_stock_jobs_use_shanghai_timezone(self) -> None:
        for workflow_name in ["stock-tasks.yml", "stock-pending.yml"]:
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
            self.assertIn("TZ: Asia/Shanghai", workflow)


if __name__ == "__main__":
    unittest.main()

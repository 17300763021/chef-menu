from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_stock_tasks import live_session_window, seconds_until_next_cycle


SHANGHAI = ZoneInfo("Asia/Shanghai")


class StockLiveSessionTest(unittest.TestCase):
    def test_morning_session_window(self) -> None:
        start, end = live_session_window(datetime(2026, 6, 18, 9, 25, tzinfo=SHANGHAI))

        self.assertEqual(start.strftime("%H:%M"), "09:30")
        self.assertEqual(end.strftime("%H:%M"), "11:30")

    def test_afternoon_session_window(self) -> None:
        start, end = live_session_window(datetime(2026, 6, 18, 12, 55, tzinfo=SHANGHAI))

        self.assertEqual(start.strftime("%H:%M"), "13:00")
        self.assertEqual(end.strftime("%H:%M"), "15:00")

    def test_outside_session_has_no_window(self) -> None:
        self.assertIsNone(live_session_window(datetime(2026, 6, 18, 15, 1, tzinfo=SHANGHAI)))

    def test_next_cycle_aligns_to_five_minute_boundary(self) -> None:
        delay = seconds_until_next_cycle(datetime(2026, 6, 18, 10, 2, 30, tzinfo=SHANGHAI))

        self.assertEqual(delay, 150)

    def test_live_engine_supports_parallel_workers(self) -> None:
        source = (ROOT / "scripts" / "stock_engine" / "a_stock_live_decision_v8.py").read_text(encoding="utf-8")

        self.assertIn('ap.add_argument("--workers"', source)
        self.assertIn("ThreadPoolExecutor", source)


if __name__ == "__main__":
    unittest.main()

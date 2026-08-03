from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.market_data.daily_catchup_runner import catch_up


class DailyCatchupTests(unittest.TestCase):
    def test_stops_after_noop_and_keeps_sessions_atomic(self) -> None:
        responses = [{"accepted": True, "dataset_id": "d1"}, {"event": "daily_noop"}]
        with tempfile.TemporaryDirectory() as tmp, patch("scripts.market_data.daily_catchup_runner.run", side_effect=responses) as mocked:
            summary = catch_up(max_sessions=5, base_history_dataset_id="base", output_dir=Path(tmp), symbol_attempts=2)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(summary["results"]), 2)

    def test_rejects_unbounded_or_failed_catchup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                catch_up(max_sessions=6, base_history_dataset_id="base", output_dir=Path(tmp), symbol_attempts=2)
            with patch("scripts.market_data.daily_catchup_runner.run", return_value={"accepted": False, "dataset_id": "bad"}):
                with self.assertRaisesRegex(RuntimeError, "stopped"):
                    catch_up(max_sessions=2, base_history_dataset_id="base", output_dir=Path(tmp), symbol_attempts=2)


if __name__ == "__main__":
    unittest.main()

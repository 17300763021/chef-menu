from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.market_data.daily_catchup_runner import catch_up
from scripts.market_data.daily_incremental_runner import (
    daily_membership_symbols,
    select_daily_shard_symbols,
)


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

    def test_blocked_session_is_normal_flow_and_does_not_advance(self) -> None:
        blocked = {
            "event": "daily_blocked",
            "accepted": False,
            "dataset_id": "blocked-day",
            "target_session": "2026-08-03",
            "blocked_symbols": ["689009"],
            "reason": "verification sources failed",
            "simulation_orders_allowed": False,
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.market_data.daily_catchup_runner.run", return_value=blocked,
        ) as mocked:
            summary = catch_up(
                max_sessions=5, base_history_dataset_id="base", output_dir=Path(tmp),
                symbol_attempts=2,
            )
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(summary["event"], "daily_catchup_blocked")
        self.assertEqual(summary["results"][0]["blocked_symbols"], ["689009"])

    def test_stable_shards_do_not_change_when_other_checkpoints_finish(self) -> None:
        membership = [(f"{value:06d}", "000300" if value < 6 else "000905") for value in range(12)]
        scope = list(daily_membership_symbols(membership))
        partitions = [set(select_daily_shard_symbols(scope, scope, index, 4)) for index in range(4)]
        self.assertEqual(set.union(*partitions), set(scope))
        self.assertEqual(sum(len(partition) for partition in partitions), len(scope))
        remaining = scope[3:]
        for index in range(4):
            self.assertEqual(
                set(select_daily_shard_symbols(scope, remaining, index, 4)),
                partitions[index] & set(remaining),
            )

    def test_parallel_capture_precedes_one_global_finalize(self) -> None:
        connection = MagicMock()
        with tempfile.TemporaryDirectory() as tmp, \
                patch("scripts.market_data.daily_catchup_runner.connect", return_value=connection), \
                patch("scripts.market_data.daily_catchup_runner.ensure_daily_schema"), \
                patch("scripts.market_data.daily_catchup_runner._capture_parallel") as capture, \
                patch("scripts.market_data.daily_catchup_runner.run", return_value={"accepted": True, "dataset_id": "d1"}) as finalize:
            summary = catch_up(
                max_sessions=1, base_history_dataset_id="base", output_dir=Path(tmp),
                symbol_attempts=2, parallel_shards=4, requested_target=date(2026, 7, 28),
            )
        capture.assert_called_once()
        self.assertTrue(finalize.call_args.kwargs["finalize_only"])
        self.assertEqual(summary["results"][0]["dataset_id"], "d1")
        connection.close.assert_called_once()

    def test_correction_is_single_session_single_shard_and_propagates_target(self) -> None:
        target = date(2026, 7, 31)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "one explicit target and one shard"):
                catch_up(
                    max_sessions=1, base_history_dataset_id="base", output_dir=Path(tmp),
                    symbol_attempts=2, parallel_shards=4, requested_target=target,
                    supersedes_dataset_id="bad-run",
                )
            with patch(
                "scripts.market_data.daily_catchup_runner.run",
                return_value={"accepted": True, "dataset_id": "replacement"},
            ) as run:
                summary = catch_up(
                    max_sessions=1, base_history_dataset_id="base", output_dir=Path(tmp),
                    symbol_attempts=2, parallel_shards=1, requested_target=target,
                    supersedes_dataset_id="bad-run",
                )
        self.assertEqual(summary["results"][0]["dataset_id"], "replacement")
        self.assertEqual(run.call_args.kwargs["requested_target"], target)
        self.assertEqual(run.call_args.kwargs["supersedes_dataset_id"], "bad-run")


if __name__ == "__main__":
    unittest.main()

"""Pinned-framework proof of a deterministic, reconciled RQAlpha run."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.simulation.m2_history_source import M2BoundedResearchInput, write_bounded_input
from scripts.simulation.rqalpha_backtest_runner import run_bounded_backtest
from scripts.test_v2_simulation_history_adapter import bounded_mapping


class RQAlphaBoundedBacktestExecutionTests(unittest.TestCase):
    def test_real_orders_fills_and_accounting_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m2-input.json"
            write_bounded_input(path, M2BoundedResearchInput.from_mapping(bounded_mapping()))
            first = run_bounded_backtest(path)
            second = run_bounded_backtest(path)

        self.assertEqual(first, second)
        self.assertTrue(first["accepted"])
        self.assertFalse(first["authoritative"])
        self.assertFalse(first["simulation_orders_allowed"])
        self.assertEqual(len(first["trades"]), 1)
        self.assertEqual(first["closing_quantities"], {"600519.XSHG": 100})
        self.assertEqual(set(first["reconciliation_differences"].values()), {"0.0000"})
        self.assertEqual(len(first["result_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()

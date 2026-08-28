"""Deterministic tests for exact dual-warehouse M4 release binding."""

from __future__ import annotations

import unittest
import json
from pathlib import Path

from scripts.strategy.baseline_contracts import load_complete_strategy_spec
from scripts.strategy.acceptance_report import build_report
from scripts.strategy.research_release_gate import BINDING_SCHEMA_VERSION, build_bound_release


HISTORY_ID = "history-full"


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        dataset_id = str(params[0])
        self.current = [self.rows[dataset_id]] if dataset_id in self.rows else []

    def fetchall(self):
        return list(self.current)


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


def _binding():
    digest = "a" * 64
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "business_date": "2026-07-28",
        "strategy_version": load_complete_strategy_spec()["strategy_version"],
        "components": {
            name: {"dataset_id": dataset_id, "manifest_sha256": digest}
            for name, dataset_id in (
                ("history", HISTORY_ID), ("daily", "daily"), ("industry", "industry"),
                ("fundamental", "fundamental"), ("index", "index"), ("flow", "flow"),
            )
        },
    }


def _market_rows():
    digest = "a" * 64
    return {
        HISTORY_ID: ("2026-07-24", 0, 0, 1, 1403, digest),
        "industry": ("2026-08-03", HISTORY_ID, 0, 0, 1, 1403, digest),
        "daily": ("2026-07-28", HISTORY_ID, 0, 0, 1, 800, 800, digest),
        "index": ("2026-07-28", 0, 0, 1, 4160, digest),
        "flow": ("2026-07-28", 1403, 0, 0, 0, 0, 1, digest),
    }


def _research_rows():
    manifest = {
        "dataset_id": "fundamental", "accepted": True, "authoritative": False,
        "simulation_orders_allowed": False, "expected_symbol_count": 1403,
        "successful_symbol_count": 1341, "excluded_symbol_count": 62,
        "failed_symbol_count": 0,
        "gates": [
            {"name": f"gate-{index}", "critical": True, "passed": True}
            for index in range(11)
        ],
    }
    return {
        "fundamental": (
            "2026-07-28", HISTORY_ID, 0, 0, 1, 1403, 1341, 62, "a" * 64,
            json.dumps(manifest, sort_keys=True),
        ),
    }


class M4ResearchReleaseGateTests(unittest.TestCase):
    def test_binds_exact_full_dual_warehouse_release(self):
        release = build_bound_release(_Connection(_market_rows()), _Connection(_research_rows()), _binding())
        self.assertTrue(release.actionable_research_ready)
        self.assertFalse(release.authoritative)
        self.assertFalse(release.simulation_orders_allowed)
        self.assertEqual(1403, release.component("fundamental").available_count)
        self.assertEqual("disabled_optional", release.component("flow").state.value)

    def test_rejects_partial_fundamental_inventory(self):
        rows = _research_rows()
        row = list(rows["fundamental"])
        row[7] = 61
        manifest = json.loads(row[9])
        manifest["excluded_symbol_count"] = 61
        row[9] = json.dumps(manifest, sort_keys=True)
        rows["fundamental"] = tuple(row)
        with self.assertRaisesRegex(RuntimeError, "1,403"):
            build_bound_release(_Connection(_market_rows()), _Connection(rows), _binding())

    def test_rejects_component_hash_mismatch(self):
        binding = _binding()
        binding["components"]["daily"]["manifest_sha256"] = "b" * 64
        with self.assertRaisesRegex(RuntimeError, "hash mismatch: daily"):
            build_bound_release(_Connection(_market_rows()), _Connection(_research_rows()), binding)

    def test_rejects_incomplete_critical_gate_inventory(self):
        rows = _research_rows()
        row = list(rows["fundamental"])
        manifest = json.loads(row[9])
        manifest["gates"][-1]["passed"] = False
        row[9] = json.dumps(manifest, sort_keys=True)
        rows["fundamental"] = tuple(row)
        with self.assertRaisesRegex(RuntimeError, "eleven critical"):
            build_bound_release(_Connection(_market_rows()), _Connection(rows), _binding())

    def test_rejects_mixed_history_lineage(self):
        rows = _market_rows()
        daily = list(rows["daily"])
        daily[1] = "another-history"
        rows["daily"] = tuple(daily)
        with self.assertRaisesRegex(RuntimeError, "pinned history baseline"):
            build_bound_release(_Connection(rows), _Connection(_research_rows()), _binding())

    def test_formal_report_binds_resolved_full_release(self):
        release = build_bound_release(_Connection(_market_rows()), _Connection(_research_rows()), _binding())
        report = build_report(require_qlib=False, research_release=release, require_full_release=True)
        self.assertEqual(release.release_id, report["research_release_id"])
        self.assertEqual(release.manifest_sha256, report["research_release_sha256"])
        self.assertFalse(report["simulation_orders_allowed"])

    def test_formal_workflow_uses_dual_warehouse_release_gate(self):
        workflow = Path(".github/workflows/m4-strategy-acceptance.yml").read_text(encoding="utf-8")
        self.assertIn("formal-release", workflow)
        self.assertIn("scripts.strategy.research_release_gate", workflow)
        self.assertIn("TIDB_RESEARCH_HOST", workflow)
        self.assertIn("TIDB_MARKET_HOST", workflow)
        self.assertIn("--require-full-release", workflow)


if __name__ == "__main__":
    unittest.main()

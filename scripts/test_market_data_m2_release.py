"""Deterministic tests for the immutable M2 release gate."""

from __future__ import annotations

import unittest
from datetime import date

from scripts.market_data.m2_release_gate import build_release


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=()):
        if query.lstrip().startswith("CREATE TABLE"):
            self.current = []
            return
        for marker, row in self.rows.items():
            if marker in query:
                self.current = [row]
                return
        raise AssertionError(f"unexpected query: {query}")

    def fetchall(self):
        return list(self.current)


class _Connection:
    def __init__(self, rows):
        self.cursor_value = _Cursor(rows)

    def cursor(self):
        return self.cursor_value


def _rows():
    digest = "a" * 64
    base = "history-full"
    return {
        "FROM m2_history_runs": (base, "2026-07-24", 0, 0, digest),
        "FROM m2_industry_runs": ("industry-full", "2026-08-03", base, 0, 0, digest),
        "FROM m2_fundamental_runs": ("fundamental-full", "2026-08-03", base, 0, 0, digest),
        "FROM m2_index_runs": ("index", "2026-08-03", 0, 0, digest),
        "FROM m2_daily_runs": ("daily", "2026-08-03", base, 0, 0, digest),
        "FROM m2_flow_runs": ("flow", "2026-08-03", 0, 0, 0, digest),
    }


class M2ReleaseGateTests(unittest.TestCase):
    def test_accepts_one_research_only_lineage(self):
        manifest = build_release(_Connection(_rows()), date(2026, 8, 3))
        self.assertTrue(manifest["accepted"])
        self.assertFalse(manifest["authoritative"])
        self.assertFalse(manifest["simulation_orders_allowed"])
        self.assertFalse(manifest["components"]["flow"]["data_available"])

    def test_rejects_mixed_history_lineage(self):
        rows = _rows()
        row = list(rows["FROM m2_daily_runs"])
        row[2] = "another-history"
        rows["FROM m2_daily_runs"] = tuple(row)
        with self.assertRaisesRegex(RuntimeError, "one history baseline"):
            build_release(_Connection(rows), date(2026, 8, 3))

    def test_rejects_stale_component(self):
        rows = _rows()
        row = list(rows["FROM m2_index_runs"])
        row[1] = "2026-07-31"
        rows["FROM m2_index_runs"] = tuple(row)
        with self.assertRaisesRegex(RuntimeError, "market lineage is stale"):
            build_release(_Connection(rows), date(2026, 8, 3))

    def test_rejects_component_that_allows_simulation_orders(self):
        rows = _rows()
        row = list(rows["FROM m2_fundamental_runs"])
        row[4] = 1
        rows["FROM m2_fundamental_runs"] = tuple(row)
        with self.assertRaisesRegex(RuntimeError, "escaped research-only"):
            build_release(_Connection(rows), date(2026, 8, 3))


if __name__ == "__main__":
    unittest.main()

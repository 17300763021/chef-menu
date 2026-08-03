from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.fundamental_contracts import FundamentalFact, FundamentalReport
from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_fundamental_store import publish_symbol_checkpoint


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.batch_sizes = []

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def execute(self, query, params=None):
        self.connection.queries.append((query, params))
    def executemany(self, query, params):
        rows = list(params)
        self.batch_sizes.append(len(rows))
        self.connection.queries.append((query, rows))
    def fetchone(self): return self.connection.existing


class Connection:
    def __init__(self, existing=None):
        self.existing = existing
        self.queries = []
        self.commits = 0
        self.rollbacks = 0
        self.last_cursor = None
    def cursor(self):
        self.last_cursor = Cursor(self)
        return self.last_cursor
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def evidence():
    report = FundamentalReport.build(
        symbol="000001", statement_type="balance", report_date="2025-12-31",
        notice_date="2026-03-21", update_date="2026-04-25", report_type="annual",
        currency="CNY", organization_type="bank", source="fixture", source_row={"v": 1},
    )
    fact = FundamentalFact(report.version_id, report.symbol, report.statement_type, report.report_date,
                           report.effective_on, "TOTAL_ASSETS", Decimal("100"))
    return report, fact


class FundamentalStoreTests(unittest.TestCase):
    def test_successful_checkpoint_batches_and_commits_atomically(self) -> None:
        report, fact = evidence()
        connection = Connection()
        publish_symbol_checkpoint(connection, dataset_id="d", symbol="000001", status="succeeded", reports=[report], facts=[fact])
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertIn(1, connection.last_cursor.batch_sizes)

    def test_successful_checkpoint_is_immutable(self) -> None:
        report, fact = evidence()
        report_hash = sha256([report.canonical()])
        fact_hash = sha256([fact.canonical()])
        same = Connection(existing=("succeeded", report_hash, fact_hash))
        publish_symbol_checkpoint(same, dataset_id="d", symbol="000001", status="succeeded", reports=[report], facts=[fact])
        self.assertEqual(same.commits, 0)
        changed = Connection(existing=("succeeded", "0" * 64, fact_hash))
        with self.assertRaisesRegex(RuntimeError, "immutable"):
            publish_symbol_checkpoint(changed, dataset_id="d", symbol="000001", status="succeeded", reports=[report], facts=[fact])

    def test_confirmed_exclusion_persists_auditable_reason(self) -> None:
        connection = Connection()
        reason = RuntimeError(
            "confirmed_delisted_source_empty_after_two_responses;out_date=2020-01-01"
        )
        publish_symbol_checkpoint(
            connection, dataset_id="d", symbol="000046", status="excluded", error=reason,
        )
        checkpoint = next(
            params for query, params in connection.queries
            if "INSERT INTO m2_fundamental_symbol_checkpoints" in query
        )
        self.assertEqual(checkpoint[2], "excluded")
        self.assertEqual(checkpoint[8], "RuntimeError")
        self.assertIn("two_responses", checkpoint[9])
        self.assertEqual(connection.commits, 1)


if __name__ == "__main__":
    unittest.main()

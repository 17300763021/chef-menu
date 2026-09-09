from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.index_bars import IndexBar
from scripts.market_data.manifest import sha256
from scripts.market_data.mysql_index_store import publish_index_run


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.connection.queries.append((query, params))

    def executemany(self, query, params):
        self.connection.queries.append((query, list(params)))

    def fetchone(self):
        return self.connection.existing


class Connection:
    def __init__(self, existing=None):
        self.existing = existing
        self.queries = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def fixture():
    primary = IndexBar(
        source="csi_official_history",
        index_code="000300",
        business_date=date(2026, 9, 8),
        open=Decimal("4000.00"),
        high=Decimal("4020.00"),
        low=Decimal("3980.00"),
        close=Decimal("4010.00"),
        volume_shares=1000,
        amount_cny=Decimal("5000000.00"),
    )
    verification = IndexBar(
        source="akshare_tencent_index",
        index_code="000300",
        business_date=date(2026, 9, 8),
        open=Decimal("4000.00"),
        high=Decimal("4020.00"),
        low=Decimal("3980.00"),
        close=Decimal("4010.00"),
        volume_shares=1000,
        amount_cny=None,
    )
    manifest = {
        "dataset_id": "index-2026-09-08",
        "schema_version": "m2-index-bars-v1",
        "business_start": "2026-09-08",
        "business_end": "2026-09-08",
        "authoritative": False,
        "simulation_orders_allowed": False,
        "accepted": True,
        "primary_row_count": 1,
        "verification_row_count": 1,
        "primary_sha256": sha256([primary.canonical()]),
        "verification_sha256": sha256([verification.canonical()]),
        "quality_sha256": "1" * 64,
    }
    return manifest, [primary], [verification]


class MySQLIndexStoreTests(unittest.TestCase):
    def test_publication_is_atomic_and_research_only(self):
        manifest, primary, verification = fixture()
        connection = Connection()
        result = publish_index_run(connection, manifest=manifest, primary=primary, verification=verification)
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        bar_batch = next(rows for query, rows in connection.queries if "INSERT INTO m2_index_bars" in query)
        self.assertEqual(len(bar_batch), 1)
        self.assertTrue(any("m2_index_runs" in query and query.lstrip().startswith("INSERT") for query, _ in connection.queries))

    def test_same_manifest_replays_but_mismatched_content_is_rejected(self):
        manifest, primary, verification = fixture()
        same = Connection(existing=(sha256(manifest),))
        result = publish_index_run(same, manifest=manifest, primary=primary, verification=verification)
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(same.commits, 0)
        self.assertEqual(same.rollbacks, 1)

        changed = Connection(existing=("0" * 64,))
        with self.assertRaisesRegex(RuntimeError, "different content"):
            publish_index_run(changed, manifest=manifest, primary=primary, verification=verification)
        self.assertEqual(changed.commits, 0)
        self.assertEqual(changed.rollbacks, 1)

    def test_unaccepted_or_authoritative_evidence_is_rejected_before_sql(self):
        manifest, primary, verification = fixture()
        for changed in (
            {**manifest, "accepted": False},
            {**manifest, "authoritative": True},
            {**manifest, "simulation_orders_allowed": True},
        ):
            connection = Connection()
            with self.assertRaisesRegex(ValueError, "accepted research-only evidence"):
                publish_index_run(connection, manifest=changed, primary=primary, verification=verification)
            self.assertEqual(connection.queries, [])


if __name__ == "__main__":
    unittest.main()

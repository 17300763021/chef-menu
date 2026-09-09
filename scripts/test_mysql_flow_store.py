from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.manifest import sha256
from scripts.market_data.mysql_flow_store import publish_flow_run
from scripts.market_data.verified_flow import VerifiedFlowFact


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.connection.queries.append((query, params))

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
    fact = VerifiedFlowFact(
        symbol="000001",
        business_date=date(2026, 9, 8),
        main_net_inflow_cny=Decimal("100.00"),
        main_net_inflow_ratio=Decimal("1.25"),
        super_large_net_inflow_cny=None,
        large_net_inflow_cny=Decimal("60.00"),
        medium_net_inflow_cny=Decimal("30.00"),
        small_net_inflow_cny=Decimal("10.00"),
    )
    manifest = {
        "dataset_id": "flow-2026-09-08",
        "schema_version": "m2-verified-flow-v1",
        "business_date": "2026-09-08",
        "expected_symbol_count": 1,
        "available_symbol_count": 1,
        "data_available": True,
        "authoritative": False,
        "simulation_orders_allowed": False,
        "boundary_accepted": True,
        "facts_sha256": sha256([fact.canonical()]),
    }
    checkpoints = [{"symbol": "000001", "status": "succeeded"}]
    return manifest, [fact], checkpoints


class MySQLFlowStoreTests(unittest.TestCase):
    def test_publication_is_atomic_and_research_only(self):
        manifest, facts, checkpoints = fixture()
        connection = Connection()
        result = publish_flow_run(connection, manifest=manifest, facts=facts, checkpoints=checkpoints)
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertTrue(any("m2_flow_facts" in query for query, _ in connection.queries))
        self.assertTrue(any("m2_flow_symbol_checkpoints" in query for query, _ in connection.queries))
        self.assertTrue(any("m2_flow_runs" in query and query.lstrip().startswith("INSERT") for query, _ in connection.queries))

    def test_same_manifest_replays_but_mismatched_content_is_rejected(self):
        manifest, facts, checkpoints = fixture()
        same = Connection(existing=(sha256(manifest),))
        result = publish_flow_run(same, manifest=manifest, facts=facts, checkpoints=checkpoints)
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(same.commits, 0)
        self.assertEqual(same.rollbacks, 1)

        changed = Connection(existing=("0" * 64,))
        with self.assertRaisesRegex(RuntimeError, "different content"):
            publish_flow_run(changed, manifest=manifest, facts=facts, checkpoints=checkpoints)
        self.assertEqual(changed.commits, 0)
        self.assertEqual(changed.rollbacks, 1)

    def test_authoritative_or_unaccepted_boundary_is_rejected_before_sql(self):
        manifest, facts, checkpoints = fixture()
        for changed in (
            {**manifest, "authoritative": True},
            {**manifest, "simulation_orders_allowed": True},
            {**manifest, "boundary_accepted": False},
        ):
            connection = Connection()
            with self.assertRaisesRegex(ValueError, "fail-closed research boundary"):
                publish_flow_run(connection, manifest=changed, facts=facts, checkpoints=checkpoints)
            self.assertEqual(connection.queries, [])


if __name__ == "__main__":
    unittest.main()

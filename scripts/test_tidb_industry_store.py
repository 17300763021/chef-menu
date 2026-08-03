from __future__ import annotations

import unittest
from collections import defaultdict
from datetime import date

from scripts.market_data.industry_classification import (
    HISTORY_START,
    build_intervals,
    build_manifest,
    enrich_interval_names,
    evaluate_industry,
)
from scripts.market_data.industry_contracts import (
    IndustryNode,
    IndustryScopeSecurity,
    IndustryVerification,
    SwsAssignmentRecord,
)
from scripts.market_data.tidb_industry_store import (
    SCHEMA_STATEMENTS,
    load_base_scope,
    publish_industry_run,
    publish_symbol_checkpoint,
    validate_publication,
)
from scripts.market_data.manifest import sha256


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.connection.calls.append((" ".join(sql.split()), params))
        if "FROM m2_history_runs" in sql:
            self.rows = [(1, 0, 0, 2)]
        elif "FROM m2_history_run_shards" in sql:
            self.rows = [
                ("000001", date(2010, 1, 1), None),
                ("000002", date(2020, 1, 1), None),
            ]
        elif "FROM m2_industry_symbol_checkpoints" in sql:
            self.rows = list(self.connection.checkpoint_rows)
        else:
            self.rows = []

    def executemany(self, sql, params):
        rows = list(params)
        self.connection.calls.append(("MANY " + " ".join(sql.split()), rows))
        self.rows = []

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, checkpoint_rows=()) -> None:
        self.calls = []
        self.checkpoint_rows = tuple(checkpoint_rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def evidence_fixture():
    observed = date(2026, 7, 28)
    scope = [
        IndustryScopeSecurity.build("000001", "2010-01-01"),
        IndustryScopeSecurity.build("000002", "2020-01-01"),
    ]
    source = [
        SwsAssignmentRecord.build(symbol="000001", source_effective_from="2014-02-21", industry_code="480101", source_updated_at="2024-01-01"),
        SwsAssignmentRecord.build(symbol="000001", source_effective_from="2021-07-30", industry_code="480301", source_updated_at="2024-01-01"),
        SwsAssignmentRecord.build(symbol="000002", source_effective_from="2019-01-01", industry_code="430101", source_updated_at="2020-01-01"),
        SwsAssignmentRecord.build(symbol="000002", source_effective_from="2021-07-30", industry_code="430201", source_updated_at="2024-01-01"),
    ]
    verifications = [
        IndustryVerification("000001", date(2014, 2, 21), "480101", "银行", "银行Ⅱ", "银行Ⅲ", "申银万国行业分类标准", "008013"),
        IndustryVerification("000001", date(2021, 7, 30), "480301", "银行", "银行Ⅱ", "银行Ⅲ", "申银万国行业分类标准", "008003"),
        IndustryVerification("000002", date(2019, 1, 1), "430101", "房地产", "开发", "住宅", "申银万国行业分类标准", "008013"),
        IndustryVerification("000002", date(2021, 7, 30), "430201", "房地产", "开发", "住宅", "申银万国行业分类标准", "008003"),
    ]
    codes = [str(value) for value in range(11, 40)] + ["43", "48"]
    nodes = [IndustryNode("S", "申银万国行业分类", "008", 0, "申银万国行业分类标准", "008003", None)]
    nodes.extend(IndustryNode(f"S{code}", f"行业{code}", "S", 1, "申银万国行业分类标准", "008003", None) for code in codes)
    nodes.extend([
        IndustryNode("S4302", "房地产开发Ⅱ", "S43", 2, "申银万国行业分类标准", "008003", None),
        IndustryNode("S430201", "住宅开发Ⅲ", "S4302", 3, "申银万国行业分类标准", "008003", None),
        IndustryNode("S4803", "股份制银行Ⅱ", "S48", 2, "申银万国行业分类标准", "008003", None),
        IndustryNode("S480301", "股份制银行Ⅲ", "S4803", 3, "申银万国行业分类标准", "008003", None),
    ])
    intervals = enrich_interval_names(
        build_intervals(scope, source, observed_on=observed, as_of_date=observed),
        verifications,
        nodes,
    )
    gates = evaluate_industry(
        scope=scope, source_rows=source, intervals=intervals,
        verifications=verifications, nodes=nodes, history_start=HISTORY_START,
        as_of_date=observed, expected_scope_count=2,
    )
    manifest = build_manifest(
        dataset_id="m2-industry-test", base_history_dataset_id="m2-base", mode="sample",
        observed_on=observed, as_of_date=observed, history_start=HISTORY_START,
        scope=scope, source_rows=source, intervals=intervals, verifications=verifications,
        nodes=nodes, gates=gates, source_metadata={"source": "fixture"},
    )
    return manifest, scope, source, intervals, verifications, nodes


def checkpoint_fixture(source, intervals, verifications):
    source_by_symbol = defaultdict(list)
    intervals_by_symbol = defaultdict(list)
    verifications_by_symbol = defaultdict(list)
    for row in source:
        source_by_symbol[row.symbol].append(row)
    for row in intervals:
        intervals_by_symbol[row.symbol].append(row)
    for row in verifications:
        verifications_by_symbol[row.symbol].append(row)
    result = []
    for symbol in sorted(source_by_symbol):
        source_payload = [row.canonical() for row in sorted(source_by_symbol[symbol], key=lambda value: value.source_effective_from)]
        interval_payload = [row.canonical() for row in sorted(intervals_by_symbol[symbol], key=lambda value: value.key)]
        verification_payload = [row.canonical() for row in sorted(verifications_by_symbol[symbol], key=lambda value: value.key)]
        result.append((
            symbol, "succeeded", len(source_payload), len(interval_payload), len(verification_payload),
            sha256(source_payload), sha256(interval_payload), sha256(verification_payload),
        ))
    return result


class TiDBIndustryStoreTest(unittest.TestCase):
    def test_schema_persists_normalized_official_source_assignments(self) -> None:
        schema = "\n".join(SCHEMA_STATEMENTS)

        self.assertIn("CREATE TABLE IF NOT EXISTS m2_industry_source_assignments", schema)
        self.assertIn("PRIMARY KEY (dataset_id, symbol, source_effective_from)", schema)
        self.assertIn("standard_code VARCHAR(32) NULL", schema)
        self.assertIn("row_sha256 CHAR(64) NOT NULL", schema)

    def test_symbol_checkpoint_rejects_cross_symbol_rows_before_database_writes(self) -> None:
        _manifest, _scope, _source, intervals, verifications, _nodes = evidence_fixture()

        with self.assertRaisesRegex(ValueError, "different symbol"):
            publish_symbol_checkpoint(
                FakeConnection(), dataset_id="m2-industry-test", symbol="000001",
                shard_index=0, source_rows=[row for row in _source if row.symbol == "000001"],
                intervals=[row for row in intervals if row.symbol == "000002"],
                verifications=[row for row in verifications if row.symbol == "000001"],
                status="succeeded",
            )

    def test_base_scope_must_reconcile_to_accepted_logical_history(self) -> None:
        scope = load_base_scope(FakeConnection(), "m2-base")
        self.assertEqual([row.symbol for row in scope], ["000001", "000002"])

    def test_publication_hashes_reconcile(self) -> None:
        manifest, scope, source, intervals, verifications, nodes = evidence_fixture()
        validate_publication(
            manifest, scope=scope, source_rows=source, intervals=intervals,
            verifications=verifications, nodes=nodes,
        )

    def test_final_publication_reconciles_each_symbol_checkpoint_hash(self) -> None:
        manifest, scope, source, intervals, verifications, nodes = evidence_fixture()
        connection = FakeConnection(checkpoint_rows=checkpoint_fixture(source, intervals, verifications))

        result = publish_industry_run(
            connection, manifest, scope=scope, source_rows=source, intervals=intervals,
            verifications=verifications, nodes=nodes,
        )

        self.assertTrue(result["accepted"])
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(connection.commits, 1)

    def test_final_publication_rejects_checkpoint_hash_mismatch(self) -> None:
        manifest, scope, source, intervals, verifications, nodes = evidence_fixture()
        checkpoints = checkpoint_fixture(source, intervals, verifications)
        checkpoints[0] = (*checkpoints[0][:-1], "0" * 64)

        with self.assertRaisesRegex(RuntimeError, "checkpoint hashes"):
            publish_industry_run(
                FakeConnection(checkpoint_rows=checkpoints), manifest, scope=scope,
                source_rows=source, intervals=intervals, verifications=verifications, nodes=nodes,
            )

    def test_symbol_checkpoint_atomically_includes_normalized_source_rows(self) -> None:
        manifest, scope, source, intervals, verifications, nodes = evidence_fixture()
        connection = FakeConnection()

        publish_symbol_checkpoint(
            connection, dataset_id=manifest["dataset_id"], symbol="000001", shard_index=0,
            source_rows=[row for row in source if row.symbol == "000001"],
            intervals=[row for row in intervals if row.symbol == "000001"],
            verifications=[row for row in verifications if row.symbol == "000001"],
            status="succeeded",
        )
        source_inserts = [
            params for sql, params in connection.calls
            if sql.startswith("MANY INSERT INTO m2_industry_source_assignments")
        ]

        self.assertEqual(len(source_inserts), 1)
        self.assertEqual(len(source_inserts[0]), 2)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)

    def test_publication_rejects_normalized_source_mismatch(self) -> None:
        manifest, scope, source, intervals, verifications, nodes = evidence_fixture()
        with self.assertRaisesRegex(RuntimeError, "physical evidence mismatch"):
            validate_publication(
                manifest, scope=scope, source_rows=source[:-1], intervals=intervals,
                verifications=verifications, nodes=nodes,
            )

    def test_publication_rejects_physical_interval_mismatch(self) -> None:
        manifest, scope, source, intervals, verifications, nodes = evidence_fixture()
        with self.assertRaisesRegex(RuntimeError, "physical evidence mismatch"):
            validate_publication(
                manifest, scope=scope, source_rows=source, intervals=intervals[:-1],
                verifications=verifications, nodes=nodes,
            )

    def test_publication_rejects_simulation_permission(self) -> None:
        manifest, scope, source, intervals, verifications, nodes = evidence_fixture()
        manifest = {**manifest, "simulation_orders_allowed": True}
        with self.assertRaisesRegex(RuntimeError, "research-only"):
            validate_publication(
                manifest, scope=scope, source_rows=source, intervals=intervals,
                verifications=verifications, nodes=nodes,
            )


if __name__ == "__main__":
    unittest.main()

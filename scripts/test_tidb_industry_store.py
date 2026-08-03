from __future__ import annotations

import json
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
    EXCLUDED_DELISTED_NO_HISTORY,
    IndustryExclusion,
    IndustryNode,
    IndustryScopeSecurity,
    IndustryVerification,
    SwsAssignmentRecord,
)
from scripts.market_data.tidb_industry_store import (
    SCHEMA_STATEMENTS,
    load_accepted_industry_nodes,
    load_base_scope,
    load_industry_exclusions,
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
        elif "SELECT dataset_id, node_count, nodes_sha256" in sql:
            self.rows = list(self.connection.accepted_catalog_rows)
        elif "FROM m2_industry_nodes" in sql:
            self.rows = list(self.connection.catalog_node_rows)
        elif "SELECT symbol, error_class, error_message" in sql:
            self.rows = [
                (row[0], row[8], row[9]) for row in self.connection.checkpoint_rows
                if row[1] == EXCLUDED_DELISTED_NO_HISTORY
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
    def __init__(self, checkpoint_rows=(), accepted_catalog_rows=(), catalog_node_rows=()) -> None:
        self.calls = []
        self.checkpoint_rows = tuple(checkpoint_rows)
        self.accepted_catalog_rows = tuple(accepted_catalog_rows)
        self.catalog_node_rows = tuple(catalog_node_rows)
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
            sha256(source_payload), sha256(interval_payload), sha256(verification_payload), None, None,
        ))
    return result


class TiDBIndustryStoreTest(unittest.TestCase):
    def test_schema_persists_normalized_official_source_assignments(self) -> None:
        schema = "\n".join(SCHEMA_STATEMENTS)

        self.assertIn("CREATE TABLE IF NOT EXISTS m2_industry_source_assignments", schema)
        self.assertIn("PRIMARY KEY (dataset_id, symbol, source_effective_from)", schema)
        self.assertIn("standard_code VARCHAR(32) NULL", schema)
        self.assertIn("row_sha256 CHAR(64) NOT NULL", schema)
        self.assertIn("excluded_security_count INT NOT NULL DEFAULT 0", schema)
        self.assertIn("excluded_securities_sha256 CHAR(64) NULL", schema)

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

    def test_same_date_accepted_catalog_is_physically_reconciled_before_reuse(self) -> None:
        nodes = sorted(evidence_fixture()[-1], key=lambda row: (row.level, row.node_code))
        node_hash = sha256([row.canonical() for row in nodes])
        connection = FakeConnection(
            accepted_catalog_rows=[("accepted-sample", len(nodes), node_hash)],
            catalog_node_rows=[(
                row.node_code, row.node_name, row.parent_code, row.level, row.standard_name,
                row.standard_code, row.termination_date, row.source,
            ) for row in nodes],
        )

        loaded = load_accepted_industry_nodes(connection, date(2026, 7, 28))

        self.assertIsNotNone(loaded)
        loaded_nodes, dataset_id = loaded
        self.assertEqual(dataset_id, "accepted-sample")
        self.assertEqual(loaded_nodes, nodes)

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
        corrupted = list(checkpoints[0])
        corrupted[7] = "0" * 64
        checkpoints[0] = tuple(corrupted)

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

    def test_delisted_exclusion_checkpoint_is_empty_canonical_and_resumable(self) -> None:
        exclusion = IndustryExclusion.build(symbol="000046", out_date="2020-09-21")
        connection = FakeConnection()

        publish_symbol_checkpoint(
            connection, dataset_id="m2-industry-test", symbol="000046", shard_index=0,
            source_rows=[], intervals=[], verifications=[],
            status=EXCLUDED_DELISTED_NO_HISTORY, exclusion=exclusion,
        )
        checkpoint_call = next(
            params for sql, params in connection.calls if sql.startswith("INSERT INTO m2_industry_symbol_checkpoints")
        )

        self.assertEqual(checkpoint_call[3], EXCLUDED_DELISTED_NO_HISTORY)
        self.assertEqual(checkpoint_call[4:10], (0, 0, 0, None, None, None))
        self.assertEqual(checkpoint_call[10], "IndustryExclusion")
        self.assertEqual(connection.commits, 1)

    def test_exclusion_checkpoint_rejects_any_fabricated_industry_evidence(self) -> None:
        _manifest, _scope, source, _intervals, _verifications, _nodes = evidence_fixture()
        exclusion = IndustryExclusion.build(symbol="000001", out_date="2020-09-21")

        with self.assertRaisesRegex(ValueError, "cannot contain evidence"):
            publish_symbol_checkpoint(
                FakeConnection(), dataset_id="m2-industry-test", symbol="000001", shard_index=0,
                source_rows=[row for row in source if row.symbol == "000001"],
                intervals=[], verifications=[], status=EXCLUDED_DELISTED_NO_HISTORY,
                exclusion=exclusion,
            )

    def test_final_publication_accepts_audited_delisted_exclusion(self) -> None:
        manifest, original_scope, source, intervals, verifications, nodes = evidence_fixture()
        active_symbol = original_scope[0].symbol
        scope = [
            original_scope[0],
            IndustryScopeSecurity.build("000046", "1994-06-30", "2020-09-21"),
        ]
        source = [row for row in source if row.symbol == active_symbol]
        intervals = [row for row in intervals if row.symbol == active_symbol]
        verifications = [row for row in verifications if row.symbol == active_symbol]
        exclusion = IndustryExclusion.build(symbol="000046", out_date="2020-09-21")
        gates = evaluate_industry(
            scope=scope, source_rows=source, intervals=intervals, verifications=verifications,
            nodes=nodes, history_start=HISTORY_START, as_of_date=date(2026, 7, 28),
            expected_scope_count=2, exclusions=[exclusion],
        )
        manifest = build_manifest(
            dataset_id=manifest["dataset_id"], base_history_dataset_id="m2-base", mode="full",
            observed_on=date(2026, 7, 28), as_of_date=date(2026, 7, 28), history_start=HISTORY_START,
            scope=scope, source_rows=source, intervals=intervals, verifications=verifications,
            nodes=nodes, gates=gates, source_metadata={"source": "fixture"}, exclusions=[exclusion],
        )
        checkpoints = checkpoint_fixture(source, intervals, verifications)
        checkpoints.append((
            "000046", EXCLUDED_DELISTED_NO_HISTORY, 0, 0, 0, None, None, None,
            "IndustryExclusion", json.dumps(exclusion.canonical(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
        connection = FakeConnection(checkpoint_rows=checkpoints)

        loaded = load_industry_exclusions(connection, manifest["dataset_id"])
        result = publish_industry_run(
            connection, manifest, scope=scope, source_rows=source, intervals=intervals,
            verifications=verifications, nodes=nodes, exclusions=loaded,
        )

        self.assertEqual(loaded, [exclusion])
        self.assertTrue(result["accepted"])

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

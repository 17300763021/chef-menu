from __future__ import annotations

import unittest
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from scripts.market_data.industry_classification import (
    HISTORY_START,
    build_intervals,
    build_manifest,
    canonical_scope,
    enrich_interval_names,
    evaluate_industry,
    write_gzip_rows,
)
from scripts.market_data.industry_contracts import (
    IndustryNode,
    IndustryScopeSecurity,
    IndustryVerification,
    SwsAssignmentRecord,
)
from scripts.market_data.quality_gates import accepted
from scripts.market_data.industry_runner import _dataset_id, _plan_seed, load_plan
from scripts.market_data.manifest import sha256
from scripts.market_data.sources.cninfo_industry_source import (
    normalize_cninfo_catalog,
    normalize_cninfo_changes,
)
from scripts.market_data.sources.sws_industry_source import normalize_sws_frame


OBSERVED_ON = date(2026, 7, 28)


def scope_fixture() -> list[IndustryScopeSecurity]:
    return [
        IndustryScopeSecurity.build("000001", "2010-01-01"),
        IndustryScopeSecurity.build("000002", "2020-01-01"),
    ]


def source_fixture() -> list[SwsAssignmentRecord]:
    return [
        SwsAssignmentRecord.build(
            symbol="000001", source_effective_from="2014-02-21", industry_code="480101",
            source_updated_at="2024-09-27 09:08:00",
        ),
        SwsAssignmentRecord.build(
            symbol="000001", source_effective_from="2021-07-30", industry_code="480301",
            source_updated_at="2025-12-15 16:33:00",
        ),
        SwsAssignmentRecord.build(
            symbol="000002", source_effective_from="2019-01-01", industry_code="430101",
            source_updated_at="2020-01-02 08:00:00",
        ),
        SwsAssignmentRecord.build(
            symbol="000002", source_effective_from="2021-07-30", industry_code="430201",
            source_updated_at="2021-07-31 08:00:00",
        ),
    ]


def verification_fixture() -> list[IndustryVerification]:
    return [
        IndustryVerification("000001", date(2014, 2, 21), "480101", "银行", "银行Ⅱ", "银行Ⅲ", "申银万国行业分类标准", "008013"),
        IndustryVerification("000001", date(2021, 7, 30), "480301", "银行", "银行Ⅱ", "银行Ⅲ", "申银万国行业分类标准", "008003"),
        IndustryVerification("000002", date(2019, 1, 1), "430101", "房地产", "房地产开发", "住宅开发", "申银万国行业分类标准", "008013"),
        IndustryVerification("000002", date(2021, 7, 30), "430201", "房地产", "房地产开发", "住宅开发", "申银万国行业分类标准", "008003"),
    ]


def node_fixture() -> list[IndustryNode]:
    codes = [str(value) for value in range(11, 40)] + ["43", "48"]
    rows = [IndustryNode("S", "申银万国行业分类", "008", 0, "申银万国行业分类标准", "008003", None)]
    rows.extend(
        IndustryNode(f"S{code}", f"行业{code}", "S", 1, "申银万国行业分类标准", "008003", None)
        for code in codes
    )
    return rows


class IndustryContractTest(unittest.TestCase):
    def test_frozen_plan_roundtrip_and_tamper_detection(self) -> None:
        scope = scope_fixture()
        source = source_fixture()
        nodes = node_fixture()
        seed = _plan_seed(
            base_history_dataset_id="m2-base", mode="sample", observed_on=OBSERVED_ON,
            scope_sha256=sha256(canonical_scope(scope)),
            sws_raw_sha256="a" * 64,
            source_assignments_sha256=sha256([row.canonical() for row in source]),
            nodes_sha256=sha256([row.canonical() for row in nodes]),
        )
        plan = {
            "plan_version": "m2-industry-plan-v1",
            "dataset_id": _dataset_id(seed),
            "expected_scope_count": len(scope),
            "scope_sha256": seed["scope_sha256"],
            "source_assignments_sha256": seed["source_assignments_sha256"],
            "nodes_sha256": seed["nodes_sha256"],
            "seed": seed,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            write_gzip_rows(root / "scope.json.gz", canonical_scope(scope))
            write_gzip_rows(root / "sws-assignments.json.gz", [row.canonical() for row in source])
            write_gzip_rows(root / "cninfo-nodes.json.gz", [row.canonical() for row in nodes])

            loaded, loaded_scope, _loaded_source, _loaded_nodes = load_plan(root)
            self.assertEqual(loaded["dataset_id"], plan["dataset_id"])
            self.assertEqual(len(loaded_scope), 2)

            write_gzip_rows(root / "scope.json.gz", canonical_scope(scope[:1]))
            with self.assertRaisesRegex(RuntimeError, "frozen-plan hash mismatch"):
                load_plan(root)

    def test_security_assignment_rejects_non_level3_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "6-digit"):
            SwsAssignmentRecord.build(
                symbol="000001", source_effective_from="2021-07-30",
                industry_code="4803", source_updated_at="2021-07-31 08:00:00",
            )

    def test_numeric_excel_code_is_normalized_without_changing_identity(self) -> None:
        row = SwsAssignmentRecord.build(
            symbol="000001", source_effective_from="2021-07-30",
            industry_code=480301.0, source_updated_at="2021-07-31 08:00:00",
        )

        self.assertEqual(row.industry_code, "480301")

    def test_build_intervals_preserves_version_and_knowledge_boundaries(self) -> None:
        intervals = build_intervals(
            scope_fixture(), source_fixture(), observed_on=OBSERVED_ON,
            as_of_date=OBSERVED_ON, history_start=HISTORY_START,
        )
        first = [row for row in intervals if row.symbol == "000001"]

        self.assertEqual(len(first), 2)
        self.assertEqual(first[0].valid_from, HISTORY_START)
        self.assertEqual(first[0].valid_to, date(2021, 7, 30))
        self.assertEqual(first[0].classification_version, "sw_pre_2021")
        self.assertEqual(first[1].classification_version, "sw_2021")
        self.assertEqual(first[1].known_from, OBSERVED_ON)
        self.assertEqual(first[1].knowledge_status, "historical_reconstructed")

    def test_names_are_enriched_without_fabricating_missing_values(self) -> None:
        intervals = build_intervals(
            scope_fixture(), source_fixture(), observed_on=OBSERVED_ON,
            as_of_date=OBSERVED_ON, history_start=HISTORY_START,
        )
        enriched = enrich_interval_names(intervals, verification_fixture(), node_fixture())

        self.assertEqual(enriched[0].level1_name, "银行")
        self.assertNotIn("未知", " ".join(row.level1_name or "" for row in enriched))

    def test_pre_2021_interval_does_not_inherit_current_taxonomy_name(self) -> None:
        intervals = build_intervals(
            scope_fixture(), source_fixture(), observed_on=OBSERVED_ON,
            as_of_date=OBSERVED_ON, history_start=HISTORY_START,
        )
        enriched = enrich_interval_names(intervals, [], node_fixture())
        first_pre_2021 = next(
            row for row in enriched
            if row.symbol == "000001" and row.classification_version == "sw_pre_2021"
        )
        first_2021 = next(
            row for row in enriched
            if row.symbol == "000001" and row.classification_version == "sw_2021"
        )

        self.assertIsNone(first_pre_2021.level1_name)
        self.assertEqual(first_2021.level1_name, "行业48")

    def test_out_of_scope_evidence_fails_closed(self) -> None:
        scope = scope_fixture()
        source = source_fixture()
        verifications = verification_fixture()
        nodes = node_fixture()
        intervals = enrich_interval_names(
            build_intervals(scope, source, observed_on=OBSERVED_ON, as_of_date=OBSERVED_ON),
            verifications, nodes,
        )
        intervals.append(replace(intervals[0], symbol="600000"))
        gates = evaluate_industry(
            scope=scope, source_rows=source, intervals=intervals,
            verifications=verifications, nodes=nodes, history_start=HISTORY_START,
            as_of_date=OBSERVED_ON, expected_scope_count=2,
        )
        membership = next(gate for gate in gates if gate.name == "evidence_scope_membership")

        self.assertFalse(membership.passed)
        self.assertFalse(accepted(gates))

    def test_complete_fixture_passes_all_critical_gates(self) -> None:
        scope = scope_fixture()
        source = source_fixture()
        verifications = verification_fixture()
        nodes = node_fixture()
        intervals = enrich_interval_names(
            build_intervals(scope, source, observed_on=OBSERVED_ON, as_of_date=OBSERVED_ON),
            verifications,
            nodes,
        )
        gates = evaluate_industry(
            scope=scope, source_rows=source, intervals=intervals,
            verifications=verifications, nodes=nodes, history_start=HISTORY_START,
            as_of_date=OBSERVED_ON, expected_scope_count=2,
        )

        self.assertTrue(accepted(gates), [gate.canonical() for gate in gates if not gate.passed])

    def test_missing_pre_cutover_assignment_fails_closed(self) -> None:
        scope = scope_fixture()
        source = [row for row in source_fixture() if not (row.symbol == "000002" and row.source_effective_from < date(2021, 7, 30))]
        intervals = enrich_interval_names(
            build_intervals(scope, source, observed_on=OBSERVED_ON, as_of_date=OBSERVED_ON),
            verification_fixture(), node_fixture(),
        )
        gates = evaluate_industry(
            scope=scope, source_rows=source, intervals=intervals,
            verifications=verification_fixture(), nodes=node_fixture(), history_start=HISTORY_START,
            as_of_date=OBSERVED_ON, expected_scope_count=2,
        )
        coverage = next(gate for gate in gates if gate.name == "point_in_time_interval_coverage")

        self.assertFalse(coverage.passed)
        self.assertFalse(accepted(gates))

    def test_manifest_is_deterministic_and_discloses_reconstruction(self) -> None:
        scope = scope_fixture()
        source = source_fixture()
        verifications = verification_fixture()
        nodes = node_fixture()
        intervals = enrich_interval_names(
            build_intervals(scope, source, observed_on=OBSERVED_ON, as_of_date=OBSERVED_ON),
            verifications, nodes,
        )
        gates = evaluate_industry(
            scope=scope, source_rows=source, intervals=intervals,
            verifications=verifications, nodes=nodes, history_start=HISTORY_START,
            as_of_date=OBSERVED_ON, expected_scope_count=2,
        )
        kwargs = dict(
            dataset_id="m2-industry-test", base_history_dataset_id="m2-base", mode="sample",
            observed_on=OBSERVED_ON, as_of_date=OBSERVED_ON, history_start=HISTORY_START,
            scope=scope, source_rows=source, intervals=intervals, verifications=verifications,
            nodes=nodes, gates=gates, source_metadata={"source": "fixture"},
        )

        first = build_manifest(**kwargs)
        second = build_manifest(**kwargs)

        self.assertEqual(first, second)
        self.assertTrue(first["accepted"])
        self.assertEqual(first["knowledge_boundary"]["historical_rows"], "historical_reconstructed")
        self.assertFalse(first["simulation_orders_allowed"])


class IndustrySourceNormalizationTest(unittest.TestCase):
    def test_sws_frame_requires_exact_scope_and_preserves_dates(self) -> None:
        frame = pd.DataFrame([
            {"股票代码": "000001", "计入日期": "2014-02-21 00:00:00", "行业代码": "480101", "更新日期": "2024-09-27 09:08:00"},
            {"股票代码": "600000", "计入日期": "2021-07-30", "行业代码": "480301", "更新日期": "2021-07-31 08:00:00"},
        ])
        rows = normalize_sws_frame(frame, ["000001"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].industry_code, "480101")
        self.assertEqual(rows[0].source_effective_from, date(2014, 2, 21))

    def test_cninfo_normalizers_keep_only_shenwan_rows(self) -> None:
        catalog = pd.DataFrame([
            {"类目编码": "S", "类目名称": "申银万国行业分类", "终止日期": None, "行业类型": "申银万国行业分类标准", "行业类型编码": "008003", "父类编码": "008", "分级": 0},
            {"类目编码": "S48", "类目名称": "银行", "终止日期": None, "行业类型": "申银万国行业分类标准", "行业类型编码": "008003", "父类编码": "S", "分级": 1},
        ])
        changes = pd.DataFrame([
            {"行业中类": "银行Ⅲ", "行业大类": "银行", "行业次类": "银行Ⅱ", "行业门类": "银行", "行业编码": "S480301", "分类标准": "申银万国行业分类标准", "分类标准编码": "008003", "证券代码": "000001", "变更日期": "2021-07-30"},
            {"行业中类": "货币金融", "行业大类": "金融", "行业次类": "银行", "行业门类": "金融", "行业编码": "J66", "分类标准": "证监会行业分类标准", "分类标准编码": "008001", "证券代码": "000001", "变更日期": "2021-07-30"},
        ])

        nodes = normalize_cninfo_catalog(catalog)
        rows = normalize_cninfo_changes(changes, "000001")

        self.assertEqual(len(nodes), 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].industry_code, "480301")
        self.assertEqual(rows[0].level1_name, "银行")

    def test_cninfo_conflicting_duplicate_rows_fail_closed(self) -> None:
        columns = (
            "行业中类", "行业大类", "行业次类", "行业门类", "行业编码",
            "分类标准", "分类标准编码", "证券代码", "变更日期",
        )
        first = dict(zip(columns, (
            "银行Ⅲ", "银行", "银行Ⅱ", "银行", "S480301",
            "申银万国行业分类标准", "008003", "000001", "2021-07-30",
        )))
        second = {**first, "行业中类": "冲突名称"}

        with self.assertRaisesRegex(RuntimeError, "conflicting industry rows"):
            normalize_cninfo_changes(pd.DataFrame([first, second]), "000001")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    EXCLUDED_DELISTED_NO_HISTORY,
    IndustryDelistingEvidence,
    IndustryExclusion,
    IndustryNode,
    IndustryScopeSecurity,
    IndustryVerification,
    SwsAssignmentRecord,
)
from scripts.market_data.quality_gates import accepted
from scripts.market_data.industry_runner import (
    _dataset_id,
    _delisted_no_history_exclusion,
    _plan_seed,
    load_plan,
    run_shard,
)
from scripts.market_data.manifest import sha256
from scripts.market_data.sources.cninfo_industry_source import (
    CninfoIndustrySource,
    assignments_from_cninfo_changes,
    normalize_cninfo_catalog,
    normalize_cninfo_changes,
)
from scripts.market_data.sources.sws_industry_source import normalize_sws_frame
from scripts.market_data.sources.exchange_delisting_source import normalize_delisting_frame
from scripts.market_data.sources.csi_index_source import load_identifier_continuities


OBSERVED_ON = date(2026, 7, 28)


def scope_fixture() -> list[IndustryScopeSecurity]:
    return [
        IndustryScopeSecurity.build("000001", "2010-01-01"),
        IndustryScopeSecurity.build("000002", "2020-01-01"),
    ]


def source_fixture() -> list[SwsAssignmentRecord]:
    rows = [
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
    return [replace(row, source="cninfo_official_api") for row in rows]


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
    rows.extend([
        IndustryNode("S43", "房地产", "S", 1, "申银万国行业分类标准", "008003", None),
        IndustryNode("S4302", "房地产开发Ⅱ", "S43", 2, "申银万国行业分类标准", "008003", None),
        IndustryNode("S430201", "住宅开发Ⅲ", "S4302", 3, "申银万国行业分类标准", "008003", None),
        IndustryNode("S48", "银行", "S", 1, "申银万国行业分类标准", "008003", None),
        IndustryNode("S4803", "股份制银行Ⅱ", "S48", 2, "申银万国行业分类标准", "008003", None),
        IndustryNode("S480301", "股份制银行Ⅲ", "S4803", 3, "申银万国行业分类标准", "008003", None),
    ])
    unique = {row.node_code: row for row in rows}
    return list(unique.values())


def delisting_fixture() -> IndustryDelistingEvidence:
    return IndustryDelistingEvidence.build(
        symbol="000046", delisted_on="2024-02-07", exchange="SZ",
        source="szse_official_delisting", security_name="*ST泛海",
    )


class IndustryContractTest(unittest.TestCase):
    def test_frozen_plan_roundtrip_and_tamper_detection(self) -> None:
        scope = scope_fixture()
        source = source_fixture()
        nodes = node_fixture()
        seed = _plan_seed(
            base_history_dataset_id="m2-base", mode="sample", observed_on=OBSERVED_ON,
            scope_sha256=sha256(canonical_scope(scope)),
            nodes_sha256=sha256([row.canonical() for row in nodes]),
            delisting_inventory_sha256=sha256([]),
            catalog_evidence_dataset_id=None,
        )
        plan = {
            "plan_version": "m2-industry-plan-v3",
            "dataset_id": _dataset_id(seed),
            "expected_scope_count": len(scope),
            "scope_sha256": seed["scope_sha256"],
            "nodes_sha256": seed["nodes_sha256"],
            "delisting_inventory_count": 0,
            "delisting_inventory_sha256": seed["delisting_inventory_sha256"],
            "seed": seed,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            write_gzip_rows(root / "scope.json.gz", canonical_scope(scope))
            write_gzip_rows(root / "cninfo-nodes.json.gz", [row.canonical() for row in nodes])
            write_gzip_rows(root / "exchange-delistings.json.gz", [])

            loaded, loaded_scope, _loaded_nodes, loaded_delistings = load_plan(root)
            self.assertEqual(loaded["dataset_id"], plan["dataset_id"])
            self.assertEqual(len(loaded_scope), 2)
            self.assertEqual(loaded_delistings, [])

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
        self.assertEqual(first_2021.level1_name, "银行")

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

    def test_delisted_security_with_confirmed_empty_history_is_explicitly_excluded(self) -> None:
        active = scope_fixture()[0]
        delisted = IndustryScopeSecurity.build("000046", "1994-06-30")
        delisting = delisting_fixture()
        scope = [active, delisted]
        source = [row for row in source_fixture() if row.symbol == active.symbol]
        verifications = [row for row in verification_fixture() if row.symbol == active.symbol]
        nodes = node_fixture()
        intervals = enrich_interval_names(
            build_intervals(scope, source, observed_on=OBSERVED_ON, as_of_date=OBSERVED_ON),
            verifications, nodes,
        )
        exclusion = _delisted_no_history_exclusion(
            delisted, as_of_date=OBSERVED_ON, delisting_evidence=delisting,
        )
        self.assertIsNotNone(exclusion)
        self.assertIsNone(_delisted_no_history_exclusion(
            delisted, as_of_date=OBSERVED_ON, confirmed_empty_responses=1,
        ))
        gates = evaluate_industry(
            scope=scope, source_rows=source, intervals=intervals,
            verifications=verifications, nodes=nodes, history_start=HISTORY_START,
            as_of_date=OBSERVED_ON, expected_scope_count=2, exclusions=[exclusion],
            delisting_evidence=[delisting],
        )

        self.assertTrue(accepted(gates), [gate.canonical() for gate in gates if not gate.passed])
        manifest = build_manifest(
            dataset_id="m2-industry-exclusion", base_history_dataset_id="m2-base", mode="full",
            observed_on=OBSERVED_ON, as_of_date=OBSERVED_ON, history_start=HISTORY_START,
            scope=scope, source_rows=source, intervals=intervals, verifications=verifications,
            nodes=nodes, gates=gates, source_metadata={"source": "fixture"}, exclusions=[exclusion],
        )
        self.assertEqual(manifest["excluded_security_count"], 1)
        self.assertEqual(manifest["excluded_securities"], [exclusion.canonical()])
        self.assertEqual(manifest["excluded_securities_sha256"], sha256([exclusion.canonical()]))

    def test_active_security_cannot_be_disguised_as_delisted_exclusion(self) -> None:
        scope = scope_fixture()
        forged = IndustryExclusion.build(symbol="000002", out_date="2020-09-21")
        self.assertIsNone(_delisted_no_history_exclusion(scope[1], as_of_date=OBSERVED_ON))
        gates = evaluate_industry(
            scope=scope, source_rows=source_fixture(), intervals=enrich_interval_names(
                build_intervals(scope, source_fixture(), observed_on=OBSERVED_ON, as_of_date=OBSERVED_ON),
                verification_fixture(), node_fixture(),
            ),
            verifications=verification_fixture(), nodes=node_fixture(), history_start=HISTORY_START,
            as_of_date=OBSERVED_ON, expected_scope_count=2, exclusions=[forged],
        )
        exclusion_gate = next(gate for gate in gates if gate.name == "delisted_no_history_exclusions")

        self.assertFalse(exclusion_gate.passed)
        self.assertFalse(accepted(gates))

    def test_official_delisting_inventory_normalization_preserves_exchange_evidence(self) -> None:
        frame = pd.DataFrame([{
            "证券代码": "000046", "证券简称": "*ST泛海",
            "上市日期": "1994-09-12", "终止上市日期": "2024-02-07",
        }])

        rows = normalize_delisting_frame(frame, exchange="SZ")

        self.assertEqual(rows, (delisting_fixture(),))

    def test_suspension_date_alone_cannot_be_used_as_delisting_evidence(self) -> None:
        frame = pd.DataFrame([{
            "证券代码": "000046", "证券简称": "*ST泛海",
            "上市日期": "1994-09-12", "暂停上市日期": "2024-02-07",
        }])
        with self.assertRaisesRegex(RuntimeError, "missing required column"):
            normalize_delisting_frame(frame, exchange="SZ")

    def test_shanghai_akshare_termination_alias_is_accepted(self) -> None:
        frame = pd.DataFrame([{
            "公司代码": "600291", "公司简称": "西水股份",
            "上市日期": "2000-07-19", "暂停上市日期": "2022-06-14",
        }])
        rows = normalize_delisting_frame(frame, exchange="SH")
        self.assertEqual(rows[0].delisted_on, date(2022, 6, 14))

    def test_shard_excludes_only_after_two_confirmed_empty_official_responses(self) -> None:
        scope = [IndustryScopeSecurity.build("000046", "1994-09-12")]
        nodes = node_fixture()
        delistings = [delisting_fixture()]
        scope_hash = sha256(canonical_scope(scope))
        nodes_hash = sha256([row.canonical() for row in nodes])
        delistings_hash = sha256([row.canonical() for row in delistings])
        seed = _plan_seed(
            base_history_dataset_id="m2-base", mode="full", observed_on=OBSERVED_ON,
            scope_sha256=scope_hash, nodes_sha256=nodes_hash,
            delisting_inventory_sha256=delistings_hash, catalog_evidence_dataset_id="accepted-sample",
        )
        plan = {
            "plan_version": "m2-industry-plan-v3", "dataset_id": _dataset_id(seed),
            "base_history_dataset_id": "m2-base", "mode": "full",
            "observed_on": OBSERVED_ON.isoformat(), "as_of_date": OBSERVED_ON.isoformat(),
            "history_start": HISTORY_START.isoformat(), "expected_scope_count": 1,
            "scope_sha256": scope_hash, "nodes_sha256": nodes_hash,
            "delisting_inventory_count": 1, "delisting_inventory_sha256": delistings_hash,
            "source_metadata": {}, "shard_count": 1, "seed": seed,
        }
        source = MagicMock()
        source.fetch_changes.return_value = ()
        checkpoint = MagicMock()
        connection = MagicMock()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            write_gzip_rows(root / "scope.json.gz", canonical_scope(scope))
            write_gzip_rows(root / "cninfo-nodes.json.gz", [row.canonical() for row in nodes])
            write_gzip_rows(root / "exchange-delistings.json.gz", [row.canonical() for row in delistings])
            with (
                patch("scripts.market_data.industry_runner.TiDBConfig.from_env", return_value=MagicMock()),
                patch("scripts.market_data.industry_runner.connect", return_value=connection),
                patch("scripts.market_data.industry_runner.ensure_industry_schema"),
                patch("scripts.market_data.industry_runner.completed_symbols", return_value=set()),
                patch("scripts.market_data.industry_runner.CninfoIndustrySource", return_value=source),
                patch("scripts.market_data.industry_runner.publish_symbol_checkpoint", checkpoint),
                patch("scripts.market_data.industry_runner.time.sleep"),
            ):
                result = run_shard(input_dir=root, shard_index=0, attempts=2)

        self.assertEqual(source.fetch_changes.call_count, 2)
        self.assertEqual(result["failed_symbols"], 0)
        self.assertEqual(result["excluded_symbols"], 1)
        kwargs = checkpoint.call_args.kwargs
        self.assertEqual(kwargs["status"], EXCLUDED_DELISTED_NO_HISTORY)
        self.assertEqual(kwargs["source_rows"], [])
        self.assertEqual(kwargs["intervals"], [])
        self.assertEqual(kwargs["verifications"], [])
        self.assertEqual(kwargs["exclusion"].confirmed_empty_responses, 2)
        self.assertEqual(kwargs["exclusion"].delisting_source, "szse_official_delisting")

    def test_shard_uses_only_frozen_identifier_continuity_for_empty_predecessor(self) -> None:
        scope = [IndustryScopeSecurity.build("300114", "2023-12-11")]
        nodes = node_fixture()
        scope_hash = sha256(canonical_scope(scope))
        nodes_hash = sha256([row.canonical() for row in nodes])
        delistings_hash = sha256([])
        seed = _plan_seed(
            base_history_dataset_id="m2-base", mode="full", observed_on=OBSERVED_ON,
            scope_sha256=scope_hash, nodes_sha256=nodes_hash,
            delisting_inventory_sha256=delistings_hash, catalog_evidence_dataset_id="accepted-sample",
        )
        plan = {
            "plan_version": "m2-industry-plan-v3", "dataset_id": _dataset_id(seed),
            "base_history_dataset_id": "m2-base", "mode": "full",
            "observed_on": OBSERVED_ON.isoformat(), "as_of_date": OBSERVED_ON.isoformat(),
            "history_start": HISTORY_START.isoformat(), "expected_scope_count": 1,
            "scope_sha256": scope_hash, "nodes_sha256": nodes_hash,
            "delisting_inventory_count": 0, "delisting_inventory_sha256": delistings_hash,
            "source_metadata": {}, "shard_count": 1, "seed": seed,
        }
        successor_rows = tuple(
            replace(row, symbol="302132") for row in verification_fixture() if row.symbol == "000001"
        )
        source = MagicMock()
        source.fetch_changes.side_effect = [(), successor_rows]
        checkpoint = MagicMock()
        connection = MagicMock()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            write_gzip_rows(root / "scope.json.gz", canonical_scope(scope))
            write_gzip_rows(root / "cninfo-nodes.json.gz", [row.canonical() for row in nodes])
            write_gzip_rows(root / "exchange-delistings.json.gz", [])
            with (
                patch("scripts.market_data.industry_runner.TiDBConfig.from_env", return_value=MagicMock()),
                patch("scripts.market_data.industry_runner.connect", return_value=connection),
                patch("scripts.market_data.industry_runner.ensure_industry_schema"),
                patch("scripts.market_data.industry_runner.completed_symbols", return_value=set()),
                patch("scripts.market_data.industry_runner.CninfoIndustrySource", return_value=source),
                patch("scripts.market_data.industry_runner.publish_symbol_checkpoint", checkpoint),
                patch("scripts.market_data.industry_runner.time.sleep"),
            ):
                result = run_shard(input_dir=root, shard_index=0, attempts=1)

        self.assertEqual(result["failed_symbols"], 0)
        self.assertEqual(source.fetch_changes.call_args_list[0].args[0], "300114")
        self.assertEqual(source.fetch_changes.call_args_list[1].args[0], "302132")
        kwargs = checkpoint.call_args.kwargs
        self.assertEqual(kwargs["status"], "succeeded")
        self.assertTrue(all(row.symbol == "300114" for row in kwargs["verifications"]))
        self.assertTrue(all(row.source == "cninfo_id_alias:1222544408:302132" for row in kwargs["verifications"]))
        self.assertTrue(all(row.source.startswith("cninfo_id_alias:1222544408:302132") for row in kwargs["source_rows"]))

    def test_frozen_identifier_continuity_is_hash_verified_and_one_to_one(self) -> None:
        continuity = load_identifier_continuities()["300114"]
        self.assertEqual(continuity.successor_symbol, "302132")
        self.assertEqual(continuity.notice_id, 1222544408)
        self.assertEqual(
            continuity.attachment_sha256,
            "dd68049c48df826848f361fd9e7b23dd20b6805144a2e5bc36e54db638611488",
        )

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
    def test_cninfo_ancestor_code_is_preserved_as_evidence_but_not_promoted(self) -> None:
        ancestor = IndustryVerification(
            symbol="601138", change_date=date(2018, 5, 14), industry_code="2705",
            level1_name="电子", level2_name="电子制造", level3_name="电子制造",
            standard_name="申银万国行业分类标准(旧)", standard_code="008018",
        )
        leaf = replace(ancestor, change_date=date(2018, 5, 28), industry_code="270501")

        assignments = assignments_from_cninfo_changes([ancestor, leaf])

        self.assertEqual(ancestor.industry_code, "2705")
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].industry_code, "270501")
        self.assertEqual(assignments[0].source_effective_from, date(2018, 5, 28))

    def test_cninfo_raw_empty_records_become_empty_history_without_hiding_schema_errors(self) -> None:
        class Response:
            def __init__(self, records):
                self.records = records

            def raise_for_status(self):
                return None

            def json(self):
                return {"records": self.records}

        class Requests:
            def __init__(self, records):
                self.records = records

            def get(self, *_args, **_kwargs):
                return Response(self.records)

            def post(self, *_args, **_kwargs):
                return Response(self.records)

        def source_for(records):
            requests = Requests(records)

            def loader(_symbol, _start, _end):
                frame = pd.DataFrame(requests.post().json()["records"])
                frame["变更日期"] = pd.to_datetime(frame["变更日期"], errors="coerce").dt.date
                return frame

            source = CninfoIndustrySource(
                catalog_loader=lambda: pd.DataFrame(), changes_loader=loader,
            )
            source._uses_default_changes = True
            source._akshare_module = SimpleNamespace(requests=requests)
            return source

        self.assertEqual(
            source_for([]).fetch_changes("000046", date(1990, 1, 1), OBSERVED_ON),
            (),
        )
        with self.assertRaisesRegex(RuntimeError, "CNINFO request failed"):
            source_for([{"unexpected": "schema"}]).fetch_changes(
                "000046", date(1990, 1, 1), OBSERVED_ON,
            )

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

        assignments = assignments_from_cninfo_changes(list(rows))
        self.assertEqual(len(assignments), 1)
        self.assertTrue(assignments[0].source.startswith("cninfo_official_api"))
        self.assertEqual(assignments[0].standard_code, "008003")

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

    def test_current_and_legacy_standards_create_a_non_leaking_2021_cutover(self) -> None:
        current = IndustryVerification(
            "000002", date(1991, 1, 29), "430101", "房地产", "房地产开发", "住宅开发",
            "申银万国行业分类标准", "008003",
        )
        legacy = replace(
            current, level3_name="房地产开发", standard_name="申银万国行业分类标准(旧)",
            standard_code="008018",
        )

        assignments = assignments_from_cninfo_changes([current, legacy])

        self.assertEqual(len(assignments), 2)
        self.assertEqual(assignments[0].source_effective_from, date(1991, 1, 29))
        self.assertEqual(assignments[0].standard_code, "008018")
        self.assertEqual(assignments[1].source_effective_from, date(2021, 7, 30))
        self.assertEqual(assignments[1].industry_code, "430101")
        self.assertEqual(assignments[1].standard_code, "008003")
        self.assertIn("cutover_normalized", assignments[1].source)

    def test_latest_current_standard_row_before_cutover_is_selected(self) -> None:
        old_current = IndustryVerification(
            "300059", date(2010, 3, 10), "470303", "非银金融", "多元金融", "金融信息服务",
            "申银万国行业分类标准", "008003",
        )
        latest_current = replace(old_current, change_date=date(2020, 7, 23), industry_code="490101")
        legacy = replace(
            latest_current, change_date=date(2020, 7, 24), standard_name="申银万国行业分类标准(旧)",
            standard_code="008018",
        )

        assignments = assignments_from_cninfo_changes([old_current, latest_current, legacy])

        self.assertEqual(
            [(row.source_effective_from, row.industry_code, row.standard_code) for row in assignments],
            [
                (date(2020, 7, 24), "490101", "008018"),
                (date(2021, 7, 30), "490101", "008003"),
            ],
        )

    def test_different_codes_on_same_cninfo_date_fail_closed(self) -> None:
        first = IndustryVerification(
            "000002", date(1991, 1, 29), "430101", "房地产", "房地产开发", "住宅开发",
            "申银万国行业分类标准", "008003",
        )
        second = replace(first, industry_code="430201")

        with self.assertRaisesRegex(RuntimeError, "conflicting primary assignment codes"):
            assignments_from_cninfo_changes([first, second])


if __name__ == "__main__":
    unittest.main()

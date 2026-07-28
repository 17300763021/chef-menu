from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

from scripts.market_data.historical_bars import _write_gzip
from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_checkpoint_store import (
    HistoricalEvidence,
    TiDBConfig,
    build_checkpoint_repair_plan,
    connect,
    default_dataset_id,
    ensure_schema,
    load_historical_evidence,
    load_historical_manifest,
    load_resumable_evidence,
    merged_shard_rows,
    publish_historical_evidence,
    publish_symbol_checkpoint,
    symbol_checkpoint_rows,
)


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.connection.executed.append((sql, params))

    def executemany(self, sql: str, rows) -> None:
        self.connection.executed_many.append((sql, list(rows)))

    def fetchall(self):
        return self.connection.query_result(self.connection.executed[-1][0])


class FakeConnection:
    def __init__(self) -> None:
        self.executed = []
        self.executed_many = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def query_result(self, sql: str):
        return []


class PhysicalShardConnection(FakeConnection):
    def __init__(self, evidence: HistoricalEvidence, *, omit_dataset_id: str | None = None) -> None:
        super().__init__()
        self.evidence = evidence
        self.omit_dataset_id = omit_dataset_id

    def query_result(self, sql: str):
        if "FROM m2_history_runs" not in sql:
            return []
        rows = []
        for shard in self.evidence.manifest["shard_manifests"]:
            dataset_id = shard["checkpoint_dataset_id"]
            if dataset_id == self.omit_dataset_id:
                continue
            rows.append((
                dataset_id,
                shard["mode"],
                shard["business_end"],
                shard["shard_index"],
                shard["shard_count"],
                0,
                0,
                1,
                shard["global_symbol_count"],
                shard["global_expected_key_count"],
                shard["symbol_count"],
                shard["expected_key_count"],
                shard["bar_count"],
                shard["tradeability_count"],
                shard["adjustment_event_count"],
                shard["reference_count"],
                shard["verification_check_count"],
                shard["bars_sha256"],
                shard["tradeability_sha256"],
                shard["adjustments_sha256"],
                shard["references_sha256"],
                shard["verification_checks_sha256"],
                sha256(shard),
            ))
        return rows


def sample_evidence(*, accepted: bool = True) -> HistoricalEvidence:
    manifest = {
        "manifest_version": "m2-historical-market-manifest-v1",
        "authoritative": False,
        "simulation_orders_allowed": False,
        "mode": "sample",
        "business_end": "2026-07-15",
        "history_start": "2026-01-01",
        "shard_index": 0,
        "shard_count": 1,
        "accepted": accepted,
        "primary_failures": {"600519": "RuntimeError: source unavailable"},
        "primary_sources_by_symbol": {"000001": "akshare_eastmoney"},
        "bars_sha256": "a" * 64,
        "tradeability_sha256": "b" * 64,
        "adjustments_sha256": "c" * 64,
    }
    return HistoricalEvidence(
        manifest=manifest,
        bars=[{
            "symbol": "000001", "exchange": "SZSE", "business_date": "2026-07-15",
            "index_code": "000300", "open": "10.0000", "high": "11.0000", "low": "9.0000",
            "close": "10.5000", "previous_close": "10.0000", "volume_shares": 1000,
            "amount_cny": "10500.00", "turnover_percent": "1.000000",
            "qfq_factor": "1.000000", "hfq_factor": "1.000000",
            "qfq_open": "10.0000", "qfq_high": "11.0000", "qfq_low": "9.0000",
            "qfq_close": "10.5000", "hfq_open": "10.0000", "hfq_high": "11.0000",
            "hfq_low": "9.0000", "hfq_close": "10.5000",
            "primary_source": "akshare_eastmoney", "factor_source": "akshare_sina_factor",
            "schema_version": "m2-historical-market-v1",
        }],
        tradeability=[
            {
                "symbol": "000001", "business_date": "2026-07-15", "index_code": "000300",
                "has_primary_bar": True, "has_secondary_status": True, "is_suspended": False,
                "is_st": False, "listing_age_sessions": 200, "limit_rate": "0.10",
                "limit_up": "11.00", "limit_down": "9.00", "at_limit_up": False,
                "at_limit_down": False, "one_price_limit_up": False,
                "one_price_limit_down": False, "can_buy": True, "can_sell": True,
                "block_reasons": [], "schema_version": "m2-tradeability-v1",
            },
            {
                "symbol": "600519", "business_date": "2026-07-15", "index_code": "000300",
                "has_primary_bar": False, "has_secondary_status": False, "is_suspended": True,
                "is_st": None, "listing_age_sessions": 200, "limit_rate": None,
                "limit_up": None, "limit_down": None, "at_limit_up": False,
                "at_limit_down": False, "one_price_limit_up": False,
                "one_price_limit_down": False, "can_buy": False, "can_sell": False,
                "block_reasons": ["missing_primary_bar"], "schema_version": "m2-tradeability-v1",
            },
        ],
        adjustments=[{
            "symbol": "000001", "effective_date": "2026-07-15",
            "qfq_factor": "1.000000", "hfq_factor": "1.000000", "source": "akshare_sina_factor",
        }],
        references=[{
            "symbol": "000001", "exchange": "SZSE", "name": "Ping An Bank",
            "ipo_date": "1991-04-03", "out_date": None, "source": "akshare_eastmoney",
        }],
        verification_checks=[],
    )


def merged_manifest_evidence() -> HistoricalEvidence:
    merge_checks = {
        "consistent_shard_metadata": True,
        "complete_shard_inventory": True,
        "all_shards_accepted": True,
        "shard_content_hashes_reconcile": True,
        "no_cross_shard_duplicates": True,
        "expected_counts_reconcile": True,
        "global_aggregate_accepted": True,
        "verification_counts_reconcile": True,
        "verification_symbol_inventory_reconciles": True,
        "cross_source_accepted": True,
    }
    shards = []
    for index in range(2):
        shards.append({
            "manifest_version": "m2-historical-market-manifest-v1",
            "authoritative": False,
            "simulation_orders_allowed": False,
            "accepted": True,
            "mode": "preflight",
            "history_start": "2018-01-01",
            "business_end": "2026-07-24",
            "shard_index": index,
            "shard_count": 2,
            "global_symbol_count": 20,
            "global_expected_key_count": 200,
            "symbol_count": 10,
            "expected_key_count": 100,
            "bar_count": 99,
            "tradeability_count": 100,
            "adjustment_event_count": 3,
            "reference_count": 10,
            "verification_check_count": 8,
            "checkpoint_dataset_id": f"physical-shard-{index}",
            "bars_sha256": "a" * 64,
            "tradeability_sha256": "b" * 64,
            "adjustments_sha256": "c" * 64,
            "references_sha256": "d" * 64,
            "verification_checks_sha256": "e" * 64,
        })
    return HistoricalEvidence(
        manifest={
            "manifest_version": "m2-historical-market-merged-manifest-v2",
            "authoritative": False,
            "simulation_orders_allowed": False,
            "accepted": True,
            "mode": "preflight",
            "business_end": "2026-07-24",
            "shard_count": 2,
            "global_symbol_count": 20,
            "global_expected_key_count": 200,
            "bar_count": 198,
            "tradeability_count": 200,
            "adjustment_event_count": 6,
            "reference_count": 20,
            "verification_check_count": 16,
            "bars_sha256": "1" * 64,
            "tradeability_sha256": "2" * 64,
            "adjustments_sha256": "3" * 64,
            "references_sha256": "4" * 64,
            "verification_checks_sha256": "5" * 64,
            "merge_checks": merge_checks,
            "shard_manifests": shards,
        },
        bars=[],
        tradeability=[],
        adjustments=[],
        references=[],
        verification_checks=[],
    )


class TiDBCheckpointStoreTests(unittest.TestCase):
    def test_repair_plan_selects_only_missing_failed_or_incomplete_checkpoints(self) -> None:
        class RepairConnection(FakeConnection):
            def query_result(self, sql: str):
                if "FROM m2_history_symbol_checkpoints" not in sql:
                    return []
                valid_hash = "a" * 64
                return [
                    ("shard-0", "000001", "full", 0, 2, "2026-01-01", "2026-07-24", "succeeded", 100, 99, 100, 0, 1, "akshare_eastmoney", valid_hash, valid_hash, None, None),
                    ("shard-0", "000002", "full", 0, 2, "2026-01-01", "2026-07-24", "failed", 100, 0, 100, 0, 0, None, None, valid_hash, "primary_failure", "endpoint closed"),
                    ("shard-1", "600001", "full", 1, 2, "2026-01-01", "2026-07-24", "succeeded", 90, 88, 90, 87, 1, "akshare_sina", valid_hash, valid_hash, None, None),
                ]

        result = build_checkpoint_repair_plan(
            RepairConnection(),
            dataset_ids={0: "shard-0", 1: "shard-1"},
            expectations={
                0: {
                    "000001": (100, False, "2026-01-01", "2026-07-24"),
                    "000002": (100, False, "2026-01-01", "2026-07-24"),
                },
                1: {"600001": (90, True, "2026-01-01", "2026-07-24")},
            },
            mode="full",
            shard_count=2,
        )

        self.assertEqual(result["resumable_symbol_count"], 1)
        self.assertEqual(result["repair_symbol_count"], 2)
        self.assertEqual(result["repair_shard_count"], 2)
        self.assertEqual(result["repair_matrix"], {
            "include": [
                {"shard_index": 0, "shard_count": 2},
                {"shard_index": 1, "shard_count": 2},
            ],
        })

    def test_config_safe_summary_does_not_expose_password(self) -> None:
        config = TiDBConfig(
            host="gateway.example.com",
            port=4000,
            user="user.root",
            password="secret-password",
            database="chef_menu_market",
        )
        self.assertEqual(config.safe_summary()["password"], "***")
        self.assertNotIn("secret-password", json.dumps(config.safe_summary()))

    def test_connect_uses_tls_for_tidb_required_ssl(self) -> None:
        captured = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return object()

        previous = sys.modules.get("pymysql")
        sys.modules["pymysql"] = types.SimpleNamespace(connect=fake_connect)
        try:
            connect(TiDBConfig(
                host="gateway.example.com",
                port=4000,
                user="user.root",
                password="secret-password",
                database="chef_menu_market",
                ssl_mode="REQUIRED",
            ))
        finally:
            if previous is None:
                sys.modules.pop("pymysql", None)
            else:
                sys.modules["pymysql"] = previous
        self.assertEqual(captured["ssl"], {"check_hostname": False})
        self.assertNotIn("secret-password", json.dumps({key: value for key, value in captured.items() if key != "password"}))

    def test_default_dataset_id_is_deterministic_and_scoped(self) -> None:
        evidence = sample_evidence()
        first = default_dataset_id(evidence.manifest)
        second = default_dataset_id(dict(reversed(list(evidence.manifest.items()))))
        self.assertEqual(first, second)
        self.assertIn("sample", first)
        self.assertIn("shard-0-of-1", first)

    def test_symbol_checkpoints_record_success_and_failure(self) -> None:
        rows = symbol_checkpoint_rows("dataset", sample_evidence(accepted=False))
        by_symbol = {row[1]: row for row in rows}
        self.assertEqual(by_symbol["000001"][8], "succeeded")
        self.assertEqual(by_symbol["600519"][8], "failed")
        self.assertEqual(by_symbol["600519"][20], "primary_failure")
        self.assertEqual(by_symbol["600519"][21], "RuntimeError: source unavailable")

    def test_publish_refuses_unaccepted_manifest_unless_checkpoint_mode(self) -> None:
        connection = FakeConnection()
        with self.assertRaisesRegex(RuntimeError, "unaccepted"):
            publish_historical_evidence(connection, sample_evidence(accepted=False), dataset_id="dataset")
        result = publish_historical_evidence(
            connection,
            sample_evidence(accepted=False),
            dataset_id="dataset",
            allow_unaccepted_checkpoint=True,
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["counts"]["symbol_checkpoints"], 2)
        self.assertEqual(connection.commits, 1)

    def test_symbol_checkpoint_does_not_publish_incomplete_run_row(self) -> None:
        connection = FakeConnection()
        evidence = sample_evidence(accepted=False)
        one_symbol = HistoricalEvidence(
            manifest={**evidence.manifest, "primary_failures": {}},
            bars=evidence.bars,
            tradeability=evidence.tradeability[:1],
            adjustments=evidence.adjustments,
            references=evidence.references,
            verification_checks=evidence.verification_checks,
        )
        counts = publish_symbol_checkpoint(connection, one_symbol, dataset_id="stable-dataset")
        self.assertEqual(counts["symbol_checkpoints"], 1)
        self.assertEqual(connection.commits, 1)
        self.assertFalse(any("m2_history_runs" in sql for sql, _params in connection.executed))

    def test_load_resumable_evidence_reads_only_succeeded_symbols(self) -> None:
        class ResumeConnection(FakeConnection):
            def query_result(self, sql: str):
                if "FROM m2_history_symbol_checkpoints" in sql:
                    return [("000001",)]
                if "FROM m2_historical_bars" in sql:
                    return [(
                        "000001", "2026-07-15", "SZSE", "000300",
                        Decimal("10.0000"), Decimal("11.0000"), Decimal("9.0000"), Decimal("10.5000"),
                        Decimal("10.0000"), 1000, Decimal("10500.00"), Decimal("1.000000"),
                        Decimal("1.000000"), Decimal("1.000000"), Decimal("10.0000"), Decimal("11.0000"),
                        Decimal("9.0000"), Decimal("10.5000"), Decimal("10.0000"), Decimal("11.0000"),
                        Decimal("9.0000"), Decimal("10.5000"), "akshare_eastmoney", "akshare_eastmoney",
                        "m2-historical-market-v1",
                    )]
                if "FROM m2_tradeability_facts" in sql:
                    return [(
                        "000001", "2026-07-15", "000300", 1, 1, 0, 0, 200,
                        Decimal("0.100000"), Decimal("11.0000"), Decimal("9.0000"),
                        0, 0, 0, 0, 1, 1, "[]", "m2-tradeability-v1",
                    )]
                if "FROM m2_adjustment_events" in sql:
                    return [("000001", "2026-07-15", Decimal("1.000000"), Decimal("1.000000"), "akshare_eastmoney")]
                if "FROM m2_security_references" in sql:
                    return [("000001", "SZSE", "Ping An Bank", "1991-04-03", None, "akshare_eastmoney")]
                if "FROM m2_history_verification_checks" in sql:
                    return [("000001", "2026-07-15", Decimal("10.5000"), Decimal("10.5000"))]
                return []

        loaded = load_resumable_evidence(ResumeConnection(), "stable-dataset")
        self.assertEqual(loaded.manifest["resumed_symbols"], ["000001"])
        self.assertEqual(loaded.bars[0]["close"], "10.5000")
        self.assertEqual(loaded.tradeability[0]["block_reasons"], [])
        self.assertEqual(loaded.verification_checks[0]["verification_close"], "10.5000")

    def test_schema_creation_is_idempotent_sql_only(self) -> None:
        connection = FakeConnection()
        ensure_schema(connection)
        self.assertGreaterEqual(len(connection.executed), 6)
        self.assertEqual(connection.commits, 1)
        self.assertTrue(all("CREATE TABLE IF NOT EXISTS" in sql for sql, _ in connection.executed))
        self.assertTrue(any("m2_history_run_shards" in sql for sql, _ in connection.executed))

    def test_manifest_only_publish_maps_shards_without_duplicate_market_rows(self) -> None:
        evidence = merged_manifest_evidence()
        connection = PhysicalShardConnection(evidence)
        result = publish_historical_evidence(
            connection,
            evidence,
            dataset_id="logical-merged",
            manifest_only=True,
        )

        self.assertEqual(result["storage_mode"], "manifest_only_shards")
        self.assertEqual(result["counts"], {
            "runs": 1,
            "shard_mappings": 2,
            "bars": 0,
            "tradeability": 0,
            "adjustments": 0,
            "references": 0,
            "verification_checks": 0,
            "symbol_checkpoints": 0,
        })
        run_params = next(params for sql, params in connection.executed if "INSERT INTO m2_history_runs" in sql)
        self.assertEqual(run_params[13:20], (20, 200, 198, 200, 6, 20, 16))
        self.assertEqual(len(connection.executed_many), 1)
        mapping_sql, mapping_rows = connection.executed_many[0]
        self.assertIn("m2_history_run_shards", mapping_sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", mapping_sql)
        self.assertEqual([row[1] for row in mapping_rows], ["physical-shard-0", "physical-shard-1"])
        self.assertFalse(any("m2_historical_bars" in sql for sql, _rows in connection.executed_many))

    def test_manifest_only_publish_is_idempotent_upsert(self) -> None:
        evidence = merged_manifest_evidence()
        connection = PhysicalShardConnection(evidence)
        for _ in range(2):
            publish_historical_evidence(
                connection,
                evidence,
                dataset_id="logical-merged",
                manifest_only=True,
            )
        self.assertEqual(connection.commits, 2)
        self.assertEqual(len(connection.executed), 4)
        self.assertEqual(len(connection.executed_many), 2)
        self.assertTrue(all(len(rows) == 2 for _sql, rows in connection.executed_many))

    def test_checkpoint_manifest_only_registers_run_without_rewriting_market_rows(self) -> None:
        evidence = sample_evidence(accepted=True)
        evidence.manifest.update({
            "symbol_count": 1,
            "expected_key_count": 1,
            "bar_count": 1,
            "tradeability_count": 1,
            "adjustment_event_count": 1,
            "reference_count": 1,
            "verification_check_count": 0,
            "checkpoint_dataset_id": "physical-shard",
            "primary_failures": {},
            "bars_sha256": sha256(evidence.bars),
            "tradeability_sha256": sha256(evidence.tradeability[:1]),
            "adjustments_sha256": sha256(evidence.adjustments),
            "references_sha256": sha256(evidence.references),
            "verification_checks_sha256": sha256(evidence.verification_checks),
        })
        evidence = HistoricalEvidence(
            manifest=evidence.manifest,
            bars=evidence.bars,
            tradeability=evidence.tradeability[:1],
            adjustments=evidence.adjustments,
            references=evidence.references,
            verification_checks=evidence.verification_checks,
        )

        class CheckpointConnection(FakeConnection):
            def query_result(self, sql: str):
                if "SUM(CASE WHEN status='succeeded'" in sql:
                    return [(1, 1, 1, 1, 1, 1, 0, 1)]
                return []

        connection = CheckpointConnection()
        result = publish_historical_evidence(
            connection,
            evidence,
            dataset_id="physical-shard",
            checkpoint_manifest_only=True,
        )

        self.assertEqual(result["storage_mode"], "checkpoint_manifest_only")
        self.assertEqual(result["counts"], {
            "runs": 1,
            "shard_mappings": 0,
            "bars": 0,
            "tradeability": 0,
            "adjustments": 0,
            "references": 0,
            "verification_checks": 0,
            "symbol_checkpoints": 0,
        })
        self.assertEqual(connection.executed_many, [])
        self.assertEqual(connection.commits, 1)

    def test_manifest_only_publish_fails_closed_on_incomplete_or_mismatched_inventory(self) -> None:
        missing_id = merged_manifest_evidence()
        missing_id.manifest["shard_manifests"][0].pop("checkpoint_dataset_id")
        with self.assertRaisesRegex(RuntimeError, "checkpoint_dataset_id"):
            merged_shard_rows("logical", missing_id.manifest)

        mismatched_count = merged_manifest_evidence()
        mismatched_count.manifest["bar_count"] = 199
        with self.assertRaisesRegex(RuntimeError, "logical counts"):
            merged_shard_rows("logical", mismatched_count.manifest)

        duplicate_index = merged_manifest_evidence()
        duplicate_index.manifest["shard_manifests"][1]["shard_index"] = 0
        with self.assertRaisesRegex(RuntimeError, "indices"):
            merged_shard_rows("logical", duplicate_index.manifest)

    def test_manifest_only_publish_requires_all_acceptance_boundaries(self) -> None:
        unaccepted = merged_manifest_evidence()
        unaccepted.manifest["accepted"] = False
        with self.assertRaisesRegex(RuntimeError, "unaccepted"):
            publish_historical_evidence(
                FakeConnection(),
                unaccepted,
                dataset_id="logical",
                manifest_only=True,
            )

        failed_gate = merged_manifest_evidence()
        failed_gate.manifest["merge_checks"]["cross_source_accepted"] = False
        with self.assertRaisesRegex(RuntimeError, "critical merge check"):
            publish_historical_evidence(
                FakeConnection(),
                failed_gate,
                dataset_id="logical",
                manifest_only=True,
            )

        authoritative_shard = merged_manifest_evidence()
        authoritative_shard.manifest["shard_manifests"][0]["authoritative"] = True
        with self.assertRaisesRegex(RuntimeError, "simulation-only boundary"):
            publish_historical_evidence(
                FakeConnection(),
                authoritative_shard,
                dataset_id="logical",
                manifest_only=True,
            )

    def test_manifest_only_publish_requires_committed_matching_physical_shards(self) -> None:
        evidence = merged_manifest_evidence()
        missing = PhysicalShardConnection(evidence, omit_dataset_id="physical-shard-1")
        with self.assertRaisesRegex(RuntimeError, "physical TiDB shard runs are missing"):
            publish_historical_evidence(
                missing,
                evidence,
                dataset_id="logical",
                manifest_only=True,
            )
        self.assertEqual(missing.commits, 0)
        self.assertFalse(any("INSERT INTO m2_history_runs" in sql for sql, _params in missing.executed))

    def test_load_historical_evidence_reads_existing_output_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = sample_evidence()
            (root / "manifest.json").write_text(json.dumps(evidence.manifest), encoding="utf-8")
            _write_gzip(root / "historical-bars.json.gz", evidence.bars)
            _write_gzip(root / "tradeability.json.gz", evidence.tradeability)
            _write_gzip(root / "adjustment-events.json.gz", evidence.adjustments)
            _write_gzip(root / "verification-checks.json.gz", evidence.verification_checks)
            (root / "security-references.json").write_text(json.dumps(evidence.references), encoding="utf-8")
            loaded = load_historical_evidence(root)
            self.assertEqual(loaded.manifest["mode"], "sample")
            self.assertEqual(len(loaded.bars), 1)
            self.assertEqual(len(loaded.tradeability), 2)

    def test_load_historical_evidence_accepts_legacy_output_without_verification_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = sample_evidence()
            (root / "manifest.json").write_text(json.dumps(evidence.manifest), encoding="utf-8")
            _write_gzip(root / "historical-bars.json.gz", evidence.bars)
            _write_gzip(root / "tradeability.json.gz", evidence.tradeability)
            _write_gzip(root / "adjustment-events.json.gz", evidence.adjustments)
            (root / "security-references.json").write_text(json.dumps(evidence.references), encoding="utf-8")
            loaded = load_historical_evidence(root)
            self.assertEqual(loaded.verification_checks, [])

    def test_load_historical_manifest_does_not_require_large_evidence_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = merged_manifest_evidence()
            (root / "manifest.json").write_text(json.dumps(evidence.manifest), encoding="utf-8")
            loaded = load_historical_manifest(root)
            self.assertEqual(loaded.manifest["shard_count"], 2)
            self.assertEqual(loaded.bars, [])

    def test_workflow_uses_fail_closed_manifest_only_merged_publication(self) -> None:
        workflow = Path(".github/workflows/market-data-history-acceptance.yml").read_text(encoding="utf-8")
        merged_step = workflow.split("- name: Publish merged accepted evidence to TiDB", 1)[1]
        merged_step, merged_upload = merged_step.split("- name: Upload merged non-authoritative evidence", 1)
        self.assertIn("if: success() && inputs.publish_tidb", merged_step)
        self.assertIn("--manifest-only", merged_step)
        self.assertNotIn("--allow-unaccepted-checkpoint", merged_step)
        self.assertIn("path: historical-market-acceptance/manifest.json", merged_upload)
        self.assertNotIn("path: historical-market-acceptance/*", merged_upload)

        self.assertIn("options: [capture, resume]", workflow)
        self.assertIn("Build TiDB repair matrix from frozen plan", workflow)
        self.assertIn("--acquisition-policy repair", workflow)
        self.assertIn("--acquisition-policy finalize", workflow)
        self.assertIn("--checkpoint-manifest-only", workflow)
        self.assertIn("max-parallel: 2", workflow.split("repair-shard:", 1)[1].split("finalize-shard:", 1)[0])


if __name__ == "__main__":
    unittest.main()

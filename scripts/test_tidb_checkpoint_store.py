from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

from scripts.market_data.historical_bars import _write_gzip
from scripts.market_data.tidb_checkpoint_store import (
    HistoricalEvidence,
    TiDBConfig,
    connect,
    default_dataset_id,
    ensure_schema,
    load_historical_evidence,
    load_resumable_evidence,
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


class TiDBCheckpointStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

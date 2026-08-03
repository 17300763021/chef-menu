"""Idempotent TiDB storage for M2 point-in-time fundamental evidence."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Iterable, Mapping

from scripts.market_data.fundamental_contracts import FundamentalFact, FundamentalReport, FundamentalVerification
from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS m2_fundamental_runs (
      dataset_id VARCHAR(160) NOT NULL PRIMARY KEY,
      schema_version VARCHAR(64) NOT NULL,
      mode VARCHAR(16) NOT NULL,
      as_of_date DATE NOT NULL,
      history_start DATE NOT NULL,
      base_history_dataset_id VARCHAR(160) NOT NULL,
      authoritative TINYINT NOT NULL,
      simulation_orders_allowed TINYINT NOT NULL,
      accepted TINYINT NOT NULL,
      expected_symbol_count INT NOT NULL,
      successful_symbol_count INT NOT NULL,
      excluded_symbol_count INT NOT NULL,
      report_count INT NOT NULL,
      fact_count INT NOT NULL,
      verification_count INT NOT NULL,
      reports_sha256 CHAR(64) NOT NULL,
      facts_sha256 CHAR(64) NOT NULL,
      quality_sha256 CHAR(64) NOT NULL,
      manifest_sha256 CHAR(64) NOT NULL,
      manifest_json LONGTEXT NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      KEY idx_m2_fundamental_runs_asof (as_of_date, accepted)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_fundamental_symbol_checkpoints (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      status VARCHAR(32) NOT NULL,
      report_count INT NOT NULL,
      fact_count INT NOT NULL,
      verification_count INT NOT NULL,
      reports_sha256 CHAR(64) NULL,
      facts_sha256 CHAR(64) NULL,
      error_class VARCHAR(128) NULL,
      error_message TEXT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol),
      KEY idx_m2_fundamental_checkpoint_status (dataset_id, status)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_fundamental_reports (
      dataset_id VARCHAR(160) NOT NULL,
      report_version_id CHAR(64) NOT NULL,
      symbol CHAR(6) NOT NULL,
      statement_type VARCHAR(16) NOT NULL,
      report_date DATE NOT NULL,
      notice_date DATE NOT NULL,
      update_date DATE NOT NULL,
      effective_on DATE NOT NULL,
      report_type VARCHAR(64) NOT NULL,
      currency VARCHAR(16) NOT NULL,
      organization_type VARCHAR(64) NOT NULL,
      source VARCHAR(128) NOT NULL,
      source_row_sha256 CHAR(64) NOT NULL,
      schema_version VARCHAR(64) NOT NULL,
      PRIMARY KEY (dataset_id, report_version_id),
      KEY idx_m2_fundamental_report_pit (dataset_id, symbol, effective_on, report_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_fundamental_facts (
      dataset_id VARCHAR(160) NOT NULL,
      report_version_id CHAR(64) NOT NULL,
      symbol CHAR(6) NOT NULL,
      statement_type VARCHAR(16) NOT NULL,
      report_date DATE NOT NULL,
      effective_on DATE NOT NULL,
      metric_code VARCHAR(64) NOT NULL,
      metric_value DECIMAL(38,6) NOT NULL,
      unit VARCHAR(16) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      PRIMARY KEY (dataset_id, report_version_id, metric_code),
      KEY idx_m2_fundamental_fact_pit (dataset_id, symbol, metric_code, effective_on)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_fundamental_verifications (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      announcement_date DATE NOT NULL,
      title VARCHAR(512) NOT NULL,
      announcement_url VARCHAR(1024) NOT NULL,
      source VARCHAR(96) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      PRIMARY KEY (dataset_id, symbol, announcement_date, row_sha256)
    )
    """,
)


def ensure_fundamental_schema(connection: Any) -> None:
    try:
        with connection.cursor() as cursor:
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_rollback(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:
        pass


def publish_symbol_checkpoint(
    connection: Any,
    *,
    dataset_id: str,
    symbol: str,
    status: str,
    reports: Iterable[FundamentalReport] = (),
    facts: Iterable[FundamentalFact] = (),
    verifications: Iterable[FundamentalVerification] = (),
    error: BaseException | None = None,
) -> None:
    report_rows = list(reports)
    fact_rows = list(facts)
    verification_rows = list(verifications)
    if status == "succeeded" and (not report_rows or not fact_rows):
        raise ValueError("successful fundamental checkpoint requires reports and facts")
    if status not in {"succeeded", "failed", "excluded"}:
        raise ValueError(f"unsupported fundamental checkpoint status: {status}")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT status,reports_sha256,facts_sha256 FROM m2_fundamental_symbol_checkpoints WHERE dataset_id=%s AND symbol=%s", (dataset_id, symbol))
            existing = cursor.fetchone()
            if existing and str(existing[0]) == "succeeded" and status != "succeeded":
                return
            proposed_report_hash = sha256([row.canonical() for row in sorted(report_rows, key=lambda value: value.key)]) if report_rows else None
            proposed_fact_hash = sha256([row.canonical() for row in sorted(fact_rows, key=lambda value: value.key)]) if fact_rows else None
            if existing and str(existing[0]) == "succeeded" and status == "succeeded":
                if str(existing[1]) != str(proposed_report_hash) or str(existing[2]) != str(proposed_fact_hash):
                    raise RuntimeError("successful fundamental checkpoint is immutable and content changed")
                connection.rollback()
                return
            cursor.execute("DELETE FROM m2_fundamental_reports WHERE dataset_id=%s AND symbol=%s", (dataset_id, symbol))
            cursor.execute("DELETE FROM m2_fundamental_facts WHERE dataset_id=%s AND symbol=%s", (dataset_id, symbol))
            cursor.execute("DELETE FROM m2_fundamental_verifications WHERE dataset_id=%s AND symbol=%s", (dataset_id, symbol))
            if report_rows:
                cursor.executemany(
                    """INSERT INTO m2_fundamental_reports VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE source_row_sha256=VALUES(source_row_sha256)""",
                    [(dataset_id, row.version_id, row.symbol, row.statement_type, row.report_date, row.notice_date,
                      row.update_date, row.effective_on, row.report_type, row.currency, row.organization_type,
                      row.source, row.source_row_sha256, row.schema_version) for row in report_rows],
                )
            if fact_rows:
                cursor.executemany(
                    """INSERT INTO m2_fundamental_facts VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE metric_value=VALUES(metric_value), row_sha256=VALUES(row_sha256)""",
                    [(dataset_id, row.report_version_id, row.symbol, row.statement_type, row.report_date,
                      row.effective_on, row.metric_code, str(row.value), row.unit, sha256(row.canonical())) for row in fact_rows],
                )
            if verification_rows:
                cursor.executemany(
                    "INSERT IGNORE INTO m2_fundamental_verifications VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [(dataset_id, row.symbol, row.announcement_date, row.title, row.announcement_url,
                      row.source, sha256(row.canonical())) for row in verification_rows],
                )
            report_hash = proposed_report_hash
            fact_hash = proposed_fact_hash
            cursor.execute(
                """INSERT INTO m2_fundamental_symbol_checkpoints
                (dataset_id,symbol,status,report_count,fact_count,verification_count,reports_sha256,facts_sha256,error_class,error_message)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status=VALUES(status),report_count=VALUES(report_count),fact_count=VALUES(fact_count),
                verification_count=VALUES(verification_count),reports_sha256=VALUES(reports_sha256),facts_sha256=VALUES(facts_sha256),
                error_class=VALUES(error_class),error_message=VALUES(error_message)""",
                (dataset_id, symbol, status, len(report_rows), len(fact_rows), len(verification_rows), report_hash, fact_hash,
                 None if error is None else type(error).__name__, None if error is None else str(error)[:2000]),
            )
        connection.commit()
    except Exception:
        _safe_rollback(connection)
        raise


def load_dataset(connection: Any, dataset_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT report_version_id,symbol,statement_type,report_date,notice_date,update_date,effective_on,report_type,currency,organization_type,source,source_row_sha256,schema_version FROM m2_fundamental_reports WHERE dataset_id=%s ORDER BY symbol,statement_type,report_date,effective_on", (dataset_id,))
        reports = [dict(zip(("report_version_id","symbol","statement_type","report_date","notice_date","update_date","effective_on","report_type","currency","organization_type","source","source_row_sha256","schema_version"), row)) for row in cursor.fetchall()]
        cursor.execute("SELECT report_version_id,symbol,statement_type,report_date,effective_on,metric_code,metric_value,unit FROM m2_fundamental_facts WHERE dataset_id=%s ORDER BY symbol,report_version_id,metric_code", (dataset_id,))
        facts = [dict(zip(("report_version_id","symbol","statement_type","report_date","effective_on","metric_code","metric_value","unit"), row)) for row in cursor.fetchall()]
        cursor.execute("SELECT symbol,status,report_count,fact_count,verification_count,error_class,error_message FROM m2_fundamental_symbol_checkpoints WHERE dataset_id=%s ORDER BY symbol", (dataset_id,))
        checkpoints = [dict(zip(("symbol","status","report_count","fact_count","verification_count","error_class","error_message"), row)) for row in cursor.fetchall()]
    return reports, facts, checkpoints


def publish_run(connection: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    if bool(manifest.get("authoritative")) or bool(manifest.get("simulation_orders_allowed")):
        raise ValueError("M2 fundamental evidence must remain research-only")
    payload = _compact(manifest)
    digest = sha256(manifest)
    dataset_id = str(manifest["dataset_id"])
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT manifest_sha256 FROM m2_fundamental_runs WHERE dataset_id=%s", (dataset_id,))
            existing = cursor.fetchone()
            if existing:
                if str(existing[0]) != digest:
                    raise RuntimeError("fundamental dataset id already has different content")
                _safe_rollback(connection)
                return {"dataset_id": dataset_id, "accepted": bool(manifest["accepted"]), "idempotent_replay": True}
            cursor.execute(
                """INSERT INTO m2_fundamental_runs
                (dataset_id,schema_version,mode,as_of_date,history_start,base_history_dataset_id,authoritative,
                simulation_orders_allowed,accepted,expected_symbol_count,successful_symbol_count,excluded_symbol_count,
                report_count,fact_count,verification_count,reports_sha256,facts_sha256,quality_sha256,manifest_sha256,manifest_json)
                VALUES (%s,%s,%s,%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (dataset_id, manifest["schema_version"], manifest["mode"], manifest["as_of_date"], manifest["history_start"],
                 manifest["base_history_dataset_id"], int(bool(manifest["accepted"])), manifest["expected_symbol_count"],
                 manifest["successful_symbol_count"], manifest["excluded_symbol_count"], manifest["report_count"],
                 manifest["fact_count"], manifest["verification_count"], manifest["reports_sha256"], manifest["facts_sha256"],
                 manifest["quality_sha256"], digest, payload),
            )
        connection.commit()
        return {"dataset_id": dataset_id, "accepted": bool(manifest["accepted"]), "idempotent_replay": False}
    except Exception:
        _safe_rollback(connection)
        raise


__all__ = ["TiDBConfig", "connect", "ensure_fundamental_schema", "publish_symbol_checkpoint", "load_dataset", "publish_run"]

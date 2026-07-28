"""Resumable TiDB checkpoints and atomic visibility for M2.5 industry evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from scripts.market_data.industry_classification import canonical_scope
from scripts.market_data.industry_contracts import (
    INDUSTRY_SCHEMA_VERSION,
    IndustryInterval,
    IndustryNode,
    IndustryScopeSecurity,
    IndustryVerification,
    SwsAssignmentRecord,
)
from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect


INDUSTRY_STORE_SCHEMA_VERSION = "m2-tidb-industry-checkpoint-v2"


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS m2_industry_runs (
      dataset_id VARCHAR(160) NOT NULL PRIMARY KEY,
      store_schema_version VARCHAR(64) NOT NULL,
      data_schema_version VARCHAR(64) NOT NULL,
      manifest_version VARCHAR(96) NOT NULL,
      base_history_dataset_id VARCHAR(160) NOT NULL,
      mode VARCHAR(32) NOT NULL,
      observed_on DATE NOT NULL,
      as_of_date DATE NOT NULL,
      history_start DATE NOT NULL,
      authoritative TINYINT NOT NULL,
      simulation_orders_allowed TINYINT NOT NULL,
      accepted TINYINT NOT NULL,
      scope_count INT NOT NULL,
      source_assignment_count INT NOT NULL,
      interval_count INT NOT NULL,
      verification_count INT NOT NULL,
      node_count INT NOT NULL,
      scope_sha256 CHAR(64) NOT NULL,
      source_assignments_sha256 CHAR(64) NOT NULL,
      intervals_sha256 CHAR(64) NOT NULL,
      verifications_sha256 CHAR(64) NOT NULL,
      nodes_sha256 CHAR(64) NOT NULL,
      quality_sha256 CHAR(64) NOT NULL,
      manifest_sha256 CHAR(64) NOT NULL,
      manifest_json LONGTEXT NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      UNIQUE KEY uq_m2_industry_observation (base_history_dataset_id, observed_on, scope_sha256),
      KEY idx_m2_industry_runs_status (accepted, observed_on)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_industry_source_assignments (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      source_effective_from DATE NOT NULL,
      industry_code CHAR(6) NOT NULL,
      source_updated_at DATETIME(6) NULL,
      source VARCHAR(64) NOT NULL,
      source_schema_version VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, source_effective_from),
      KEY idx_m2_industry_source_symbol (dataset_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_industry_symbol_checkpoints (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      shard_index INT NOT NULL,
      status VARCHAR(32) NOT NULL,
      interval_count INT NOT NULL,
      verification_count INT NOT NULL,
      intervals_sha256 CHAR(64) NULL,
      verifications_sha256 CHAR(64) NULL,
      error_class VARCHAR(128) NULL,
      error_message TEXT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol),
      KEY idx_m2_industry_checkpoint_status (dataset_id, status, shard_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_industry_assignments (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      source_effective_from DATE NOT NULL,
      valid_from DATE NOT NULL,
      valid_to DATE NOT NULL,
      industry_code CHAR(6) NOT NULL,
      level1_code CHAR(2) NOT NULL,
      level2_code CHAR(4) NOT NULL,
      level3_code CHAR(6) NOT NULL,
      level1_name VARCHAR(255) NULL,
      level2_name VARCHAR(255) NULL,
      level3_name VARCHAR(255) NULL,
      classification_version VARCHAR(32) NOT NULL,
      knowledge_status VARCHAR(32) NOT NULL,
      known_from DATE NOT NULL,
      source_updated_at DATETIME(6) NULL,
      primary_source VARCHAR(64) NOT NULL,
      source_schema_version VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, valid_from),
      KEY idx_m2_industry_assignment_lookup (symbol, valid_from, valid_to),
      KEY idx_m2_industry_assignment_l1 (dataset_id, level1_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_industry_verifications (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      change_date DATE NOT NULL,
      industry_code CHAR(6) NOT NULL,
      level1_name VARCHAR(255) NULL,
      level2_name VARCHAR(255) NULL,
      level3_name VARCHAR(255) NULL,
      standard_name VARCHAR(255) NOT NULL,
      standard_code VARCHAR(32) NOT NULL,
      source VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, change_date, industry_code, standard_code),
      KEY idx_m2_industry_verification_symbol (dataset_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_industry_nodes (
      dataset_id VARCHAR(160) NOT NULL,
      node_code VARCHAR(16) NOT NULL,
      node_name VARCHAR(255) NOT NULL,
      parent_code VARCHAR(16) NULL,
      node_level INT NOT NULL,
      standard_name VARCHAR(255) NOT NULL,
      standard_code VARCHAR(32) NOT NULL,
      termination_date DATE NULL,
      source VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, node_code)
    )
    """,
)


def ensure_industry_schema(connection: Any) -> None:
    with connection.cursor() as cursor:
        for statement in SCHEMA_STATEMENTS:
            cursor.execute(statement)
    connection.commit()


def _query_all(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def load_base_scope(connection: Any, base_history_dataset_id: str) -> list[IndustryScopeSecurity]:
    runs = _query_all(connection, """
        SELECT accepted, authoritative, simulation_orders_allowed, global_symbol_count
        FROM m2_history_runs WHERE dataset_id=%s
    """, (base_history_dataset_id,))
    if len(runs) != 1:
        raise RuntimeError("base M2.3 logical history dataset is missing or ambiguous")
    accepted_flag, authoritative, simulation_allowed, expected_count = runs[0]
    if int(accepted_flag) != 1 or int(authoritative) != 0 or int(simulation_allowed) != 0:
        raise RuntimeError("base M2.3 dataset violates the accepted research-only boundary")
    rows = _query_all(connection, """
        SELECT refs.symbol, refs.ipo_date, refs.out_date
        FROM m2_history_run_shards AS shards
        JOIN m2_security_references AS refs ON refs.dataset_id=shards.shard_dataset_id
        WHERE shards.merged_dataset_id=%s AND shards.accepted=1
        ORDER BY refs.symbol
    """, (base_history_dataset_id,))
    scope = [IndustryScopeSecurity.build(row[0], row[1], row[2]) for row in rows]
    symbols = [item.symbol for item in scope]
    if len(symbols) != len(set(symbols)) or len(scope) != int(expected_count):
        raise RuntimeError(
            f"base M2.3 security scope does not reconcile: expected={expected_count} rows={len(scope)} unique={len(set(symbols))}"
        )
    return scope


def completed_symbols(connection: Any, dataset_id: str) -> set[str]:
    return {
        str(row[0]) for row in _query_all(connection, """
            SELECT symbol FROM m2_industry_symbol_checkpoints
            WHERE dataset_id=%s AND status='succeeded' ORDER BY symbol
        """, (dataset_id,))
    }


def _accepted_manifest_hash(connection: Any, dataset_id: str) -> str | None:
    rows = _query_all(connection, """
        SELECT manifest_sha256 FROM m2_industry_runs WHERE dataset_id=%s AND accepted=1
    """, (dataset_id,))
    return None if not rows else str(rows[0][0])


CHECKPOINT_UPSERT = """
INSERT INTO m2_industry_symbol_checkpoints (
  dataset_id, symbol, shard_index, status, interval_count, verification_count,
  intervals_sha256, verifications_sha256, error_class, error_message
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
  shard_index=VALUES(shard_index), status=VALUES(status),
  interval_count=VALUES(interval_count), verification_count=VALUES(verification_count),
  intervals_sha256=VALUES(intervals_sha256), verifications_sha256=VALUES(verifications_sha256),
  error_class=VALUES(error_class), error_message=VALUES(error_message)
"""


ASSIGNMENT_INSERT = """
INSERT INTO m2_industry_assignments (
  dataset_id, symbol, source_effective_from, valid_from, valid_to, industry_code,
  level1_code, level2_code, level3_code, level1_name, level2_name, level3_name,
  classification_version, knowledge_status, known_from, source_updated_at,
  primary_source, source_schema_version, row_sha256
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


VERIFICATION_INSERT = """
INSERT INTO m2_industry_verifications (
  dataset_id, symbol, change_date, industry_code, level1_name, level2_name, level3_name,
  standard_name, standard_code, source, row_sha256
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def publish_symbol_checkpoint(
    connection: Any,
    *,
    dataset_id: str,
    symbol: str,
    shard_index: int,
    intervals: Sequence[IndustryInterval],
    verifications: Sequence[IndustryVerification],
    status: str,
    error: Exception | str | None = None,
) -> None:
    if status not in {"succeeded", "failed"}:
        raise ValueError("industry checkpoint status must be succeeded or failed")
    if any(row.symbol != symbol for row in intervals):
        raise ValueError("industry interval checkpoint contains a different symbol")
    if any(row.symbol != symbol for row in verifications):
        raise ValueError("industry verification checkpoint contains a different symbol")
    if _accepted_manifest_hash(connection, dataset_id) is not None:
        raise RuntimeError("accepted industry evidence is immutable")
    interval_rows = [row.canonical() for row in intervals]
    verification_rows = [row.canonical() for row in verifications]
    interval_hash = sha256(interval_rows) if interval_rows else None
    verification_hash = sha256(verification_rows) if verification_rows else None
    error_class = None
    error_message = None
    if error is not None:
        error_class = type(error).__name__ if isinstance(error, BaseException) else "RuntimeError"
        error_message = str(error)[:4000]
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m2_industry_assignments WHERE dataset_id=%s AND symbol=%s",
                (dataset_id, symbol),
            )
            cursor.execute(
                "DELETE FROM m2_industry_verifications WHERE dataset_id=%s AND symbol=%s",
                (dataset_id, symbol),
            )
            if intervals:
                cursor.executemany(ASSIGNMENT_INSERT, [(
                    dataset_id, row.symbol, row.source_effective_from, row.valid_from, row.valid_to,
                    row.industry_code, row.level1_code, row.level2_code, row.level3_code,
                    row.level1_name, row.level2_name, row.level3_name,
                    row.classification_version, row.knowledge_status, row.known_from,
                    row.source_updated_at, row.primary_source, row.schema_version, sha256(row.canonical()),
                ) for row in intervals])
            if verifications:
                cursor.executemany(VERIFICATION_INSERT, [(
                    dataset_id, row.symbol, row.change_date, row.industry_code,
                    row.level1_name, row.level2_name, row.level3_name,
                    row.standard_name, row.standard_code, row.source, sha256(row.canonical()),
                ) for row in verifications])
            cursor.execute(CHECKPOINT_UPSERT, (
                dataset_id, symbol, shard_index, status, len(intervals), len(verifications),
                interval_hash, verification_hash, error_class, error_message,
            ))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def load_industry_intervals(connection: Any, dataset_id: str) -> list[IndustryInterval]:
    rows = _query_all(connection, """
        SELECT symbol, source_effective_from, valid_from, valid_to, industry_code,
               level1_code, level2_code, level3_code, level1_name, level2_name, level3_name,
               classification_version, knowledge_status, known_from, source_updated_at,
               primary_source, source_schema_version
        FROM m2_industry_assignments WHERE dataset_id=%s ORDER BY symbol, valid_from
    """, (dataset_id,))
    return [IndustryInterval(
        symbol=str(row[0]), source_effective_from=_date(row[1]), valid_from=_date(row[2]), valid_to=_date(row[3]),
        industry_code=str(row[4]), level1_code=str(row[5]), level2_code=str(row[6]), level3_code=str(row[7]),
        level1_name=None if row[8] is None else str(row[8]),
        level2_name=None if row[9] is None else str(row[9]),
        level3_name=None if row[10] is None else str(row[10]),
        classification_version=str(row[11]), knowledge_status=str(row[12]), known_from=_date(row[13]),
        source_updated_at=None if row[14] is None else row[14], primary_source=str(row[15]), schema_version=str(row[16]),
    ) for row in rows]


def load_industry_verifications(connection: Any, dataset_id: str) -> list[IndustryVerification]:
    rows = _query_all(connection, """
        SELECT symbol, change_date, industry_code, level1_name, level2_name, level3_name,
               standard_name, standard_code, source
        FROM m2_industry_verifications
        WHERE dataset_id=%s ORDER BY symbol, change_date, industry_code, standard_code
    """, (dataset_id,))
    return [IndustryVerification(
        symbol=str(row[0]), change_date=_date(row[1]), industry_code=str(row[2]),
        level1_name=None if row[3] is None else str(row[3]),
        level2_name=None if row[4] is None else str(row[4]),
        level3_name=None if row[5] is None else str(row[5]),
        standard_name=str(row[6]), standard_code=str(row[7]), source=str(row[8]),
    ) for row in rows]


NODE_INSERT = """
INSERT INTO m2_industry_nodes (
  dataset_id, node_code, node_name, parent_code, node_level, standard_name,
  standard_code, termination_date, source, row_sha256
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


SOURCE_ASSIGNMENT_INSERT = """
INSERT INTO m2_industry_source_assignments (
  dataset_id, symbol, source_effective_from, industry_code, source_updated_at,
  source, source_schema_version, row_sha256
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
"""


RUN_INSERT = """
INSERT INTO m2_industry_runs (
  dataset_id, store_schema_version, data_schema_version, manifest_version,
  base_history_dataset_id, mode, observed_on, as_of_date, history_start,
  authoritative, simulation_orders_allowed, accepted, scope_count,
  source_assignment_count, interval_count, verification_count, node_count,
  scope_sha256, source_assignments_sha256, intervals_sha256, verifications_sha256,
  nodes_sha256, quality_sha256, manifest_sha256, manifest_json
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def validate_publication(
    manifest: Mapping[str, Any],
    *,
    scope: Sequence[IndustryScopeSecurity],
    source_rows: Sequence[SwsAssignmentRecord],
    intervals: Sequence[IndustryInterval],
    verifications: Sequence[IndustryVerification],
    nodes: Sequence[IndustryNode],
) -> None:
    if manifest.get("accepted") is not True:
        raise RuntimeError("refusing to publish rejected industry evidence")
    if manifest.get("authoritative") is not False or manifest.get("simulation_orders_allowed") is not False:
        raise RuntimeError("industry evidence must remain research-only and non-authoritative")
    expected = {
        "scope_count": len(scope),
        "source_assignment_count": len(source_rows),
        "interval_count": len(intervals),
        "verification_count": len(verifications),
        "node_count": len(nodes),
        "scope_sha256": sha256(canonical_scope(scope)),
        "source_assignments_sha256": sha256([
            row.canonical() for row in sorted(
                source_rows, key=lambda value: (value.symbol, value.source_effective_from)
            )
        ]),
        "intervals_sha256": sha256([
            row.canonical() for row in sorted(intervals, key=lambda value: value.key)
        ]),
        "verifications_sha256": sha256([
            row.canonical() for row in sorted(verifications, key=lambda value: value.key)
        ]),
        "nodes_sha256": sha256([
            row.canonical() for row in sorted(nodes, key=lambda value: (value.level, value.node_code))
        ]),
    }
    mismatches = {key: {"manifest": manifest.get(key), "physical": value} for key, value in expected.items() if manifest.get(key) != value}
    if mismatches:
        raise RuntimeError(f"industry physical evidence mismatch: {_compact(mismatches)}")


def publish_industry_run(
    connection: Any,
    manifest: Mapping[str, Any],
    *,
    scope: Sequence[IndustryScopeSecurity],
    source_rows: Sequence[SwsAssignmentRecord],
    intervals: Sequence[IndustryInterval],
    verifications: Sequence[IndustryVerification],
    nodes: Sequence[IndustryNode],
) -> dict[str, Any]:
    validate_publication(
        manifest, scope=scope, source_rows=source_rows, intervals=intervals,
        verifications=verifications, nodes=nodes,
    )
    dataset_id = str(manifest["dataset_id"])
    manifest_hash = sha256(manifest)
    existing_hash = _accepted_manifest_hash(connection, dataset_id)
    if existing_hash is not None:
        if existing_hash == manifest_hash:
            return {"dataset_id": dataset_id, "accepted": True, "idempotent_replay": True}
        raise RuntimeError("accepted industry dataset id already exists with a different manifest")
    other = _query_all(connection, """
        SELECT dataset_id FROM m2_industry_runs
        WHERE base_history_dataset_id=%s AND observed_on=%s AND scope_sha256=%s AND accepted=1
    """, (
        manifest["base_history_dataset_id"], manifest["observed_on"], manifest["scope_sha256"],
    ))
    if other:
        raise RuntimeError("a different accepted industry observation already exists for this date")
    checkpoints = _query_all(connection, """
        SELECT symbol, status FROM m2_industry_symbol_checkpoints
        WHERE dataset_id=%s ORDER BY symbol
    """, (dataset_id,))
    completed = {str(row[0]) for row in checkpoints if str(row[1]) == "succeeded"}
    expected_symbols = {item.symbol for item in scope}
    if completed != expected_symbols:
        raise RuntimeError("industry checkpoint inventory does not reconcile to the frozen scope")
    try:
        with connection.cursor() as cursor:
            cursor.executemany(SOURCE_ASSIGNMENT_INSERT, [(
                dataset_id, row.symbol, row.source_effective_from, row.industry_code,
                row.source_updated_at, "sws_official", INDUSTRY_SCHEMA_VERSION,
                sha256(row.canonical()),
            ) for row in source_rows])
            cursor.executemany(NODE_INSERT, [(
                dataset_id, row.node_code, row.node_name, row.parent_code, row.level,
                row.standard_name, row.standard_code, row.termination_date, row.source,
                sha256(row.canonical()),
            ) for row in nodes])
            cursor.execute(RUN_INSERT, (
                dataset_id, INDUSTRY_STORE_SCHEMA_VERSION, INDUSTRY_SCHEMA_VERSION,
                manifest["manifest_version"], manifest["base_history_dataset_id"], manifest["mode"],
                manifest["observed_on"], manifest["as_of_date"], manifest["history_start"],
                0, 0, 1, manifest["scope_count"], manifest["source_assignment_count"],
                manifest["interval_count"], manifest["verification_count"], manifest["node_count"],
                manifest["scope_sha256"], manifest["source_assignments_sha256"],
                manifest["intervals_sha256"], manifest["verifications_sha256"],
                manifest["nodes_sha256"], manifest["quality_sha256"], manifest_hash,
                _compact(manifest),
            ))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"dataset_id": dataset_id, "accepted": True, "idempotent_replay": False}


__all__ = [
    "INDUSTRY_STORE_SCHEMA_VERSION", "TiDBConfig", "completed_symbols", "connect",
    "ensure_industry_schema", "load_base_scope", "load_industry_intervals",
    "load_industry_verifications", "publish_industry_run", "publish_symbol_checkpoint",
    "validate_publication",
]

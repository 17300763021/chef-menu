"""Atomic TiDB publication for compact CSI benchmark histories."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from scripts.market_data.index_bars import IndexBar
from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS m2_index_runs (
      dataset_id VARCHAR(160) NOT NULL PRIMARY KEY,
      schema_version VARCHAR(64) NOT NULL,
      business_start DATE NOT NULL,
      business_end DATE NOT NULL,
      authoritative TINYINT NOT NULL,
      simulation_orders_allowed TINYINT NOT NULL,
      accepted TINYINT NOT NULL,
      primary_row_count INT NOT NULL,
      verification_row_count INT NOT NULL,
      primary_sha256 CHAR(64) NOT NULL,
      verification_sha256 CHAR(64) NOT NULL,
      quality_sha256 CHAR(64) NOT NULL,
      manifest_sha256 CHAR(64) NOT NULL,
      manifest_json LONGTEXT NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      KEY idx_m2_index_runs_end (business_end, accepted)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_index_bars (
      dataset_id VARCHAR(160) NOT NULL,
      source_role VARCHAR(16) NOT NULL,
      source VARCHAR(96) NOT NULL,
      index_code CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      open_price DECIMAL(20,4) NOT NULL,
      high_price DECIMAL(20,4) NOT NULL,
      low_price DECIMAL(20,4) NOT NULL,
      close_price DECIMAL(20,4) NOT NULL,
      volume_shares BIGINT NULL,
      amount_cny DECIMAL(30,2) NULL,
      schema_version VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      PRIMARY KEY (dataset_id, source_role, index_code, business_date),
      KEY idx_m2_index_bar_lookup (index_code, business_date, source_role)
    )
    """,
)


def ensure_index_schema(connection: Any) -> None:
    try:
        with connection.cursor() as cursor:
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def publish_index_run(
    connection: Any,
    *,
    manifest: Mapping[str, Any],
    primary: Sequence[IndexBar],
    verification: Sequence[IndexBar],
) -> dict[str, Any]:
    if not manifest.get("accepted") or manifest.get("authoritative") or manifest.get("simulation_orders_allowed"):
        raise ValueError("index publication requires accepted research-only evidence")
    dataset_id = str(manifest["dataset_id"])
    digest = sha256(manifest)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT manifest_sha256 FROM m2_index_runs WHERE dataset_id=%s", (dataset_id,))
            existing = cursor.fetchone()
            if existing:
                if str(existing[0]) != digest:
                    raise RuntimeError("index dataset id already has different content")
                connection.rollback()
                return {"dataset_id": dataset_id, "accepted": True, "idempotent_replay": True}
            for role, rows in (("primary", primary), ("verification", verification)):
                if rows:
                    cursor.executemany(
                        """INSERT INTO m2_index_bars VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE row_sha256=VALUES(row_sha256)""",
                        [(dataset_id, role, row.source, row.index_code, row.business_date, str(row.open), str(row.high),
                          str(row.low), str(row.close), row.volume_shares,
                          None if row.amount_cny is None else str(row.amount_cny), row.schema_version, sha256(row.canonical()))
                         for row in rows],
                    )
            cursor.execute(
                """INSERT INTO m2_index_runs VALUES
                (%s,%s,%s,%s,0,0,1,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP(6))""",
                (dataset_id, manifest["schema_version"], manifest["business_start"], manifest["business_end"],
                 manifest["primary_row_count"], manifest["verification_row_count"], manifest["primary_sha256"],
                 manifest["verification_sha256"], manifest["quality_sha256"], digest,
                 json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
        connection.commit()
        return {"dataset_id": dataset_id, "accepted": True, "idempotent_replay": False}
    except Exception:
        connection.rollback()
        raise


__all__ = ["TiDBConfig", "connect", "ensure_index_schema", "publish_index_run"]

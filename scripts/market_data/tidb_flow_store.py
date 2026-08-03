"""TiDB audit storage for verified or explicitly unavailable capital-flow evidence."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect
from scripts.market_data.verified_flow import VerifiedFlowFact


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS m2_flow_runs (
      dataset_id VARCHAR(160) NOT NULL PRIMARY KEY,
      schema_version VARCHAR(64) NOT NULL,
      business_date DATE NOT NULL,
      expected_symbol_count INT NOT NULL,
      available_symbol_count INT NOT NULL,
      data_available TINYINT NOT NULL,
      authoritative TINYINT NOT NULL,
      simulation_orders_allowed TINYINT NOT NULL,
      boundary_accepted TINYINT NOT NULL,
      facts_sha256 CHAR(64) NOT NULL,
      manifest_sha256 CHAR(64) NOT NULL,
      manifest_json LONGTEXT NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      KEY idx_m2_flow_runs_date (business_date, boundary_accepted)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_flow_symbol_checkpoints (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      status VARCHAR(32) NOT NULL,
      error_class VARCHAR(128) NULL,
      error_message TEXT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_flow_facts (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      main_net_inflow_cny DECIMAL(30,2) NULL,
      main_net_inflow_ratio DECIMAL(20,6) NULL,
      super_large_net_inflow_cny DECIMAL(30,2) NULL,
      large_net_inflow_cny DECIMAL(30,2) NULL,
      medium_net_inflow_cny DECIMAL(30,2) NULL,
      small_net_inflow_cny DECIMAL(30,2) NULL,
      source VARCHAR(96) NOT NULL,
      schema_version VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      PRIMARY KEY (dataset_id, symbol, business_date)
    )
    """,
)


def ensure_flow_schema(connection: Any) -> None:
    try:
        with connection.cursor() as cursor:
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def publish_flow_run(
    connection: Any,
    *,
    manifest: Mapping[str, Any],
    facts: Sequence[VerifiedFlowFact],
    checkpoints: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if manifest.get("authoritative") or manifest.get("simulation_orders_allowed") or not manifest.get("boundary_accepted"):
        raise ValueError("flow publication must be a fail-closed research boundary")
    dataset_id = str(manifest["dataset_id"])
    digest = sha256(manifest)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT manifest_sha256 FROM m2_flow_runs WHERE dataset_id=%s", (dataset_id,))
            existing = cursor.fetchone()
            if existing:
                if str(existing[0]) != digest:
                    raise RuntimeError("flow dataset id already has different content")
                connection.rollback()
                return {"dataset_id": dataset_id, "idempotent_replay": True}
            for fact in facts:
                cursor.execute(
                    "INSERT INTO m2_flow_facts VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (dataset_id, fact.symbol, fact.business_date,
                     None if fact.main_net_inflow_cny is None else str(fact.main_net_inflow_cny),
                     None if fact.main_net_inflow_ratio is None else str(fact.main_net_inflow_ratio),
                     None if fact.super_large_net_inflow_cny is None else str(fact.super_large_net_inflow_cny),
                     None if fact.large_net_inflow_cny is None else str(fact.large_net_inflow_cny),
                     None if fact.medium_net_inflow_cny is None else str(fact.medium_net_inflow_cny),
                     None if fact.small_net_inflow_cny is None else str(fact.small_net_inflow_cny),
                     fact.source, fact.schema_version, sha256(fact.canonical())),
                )
            for row in checkpoints:
                cursor.execute(
                    "INSERT INTO m2_flow_symbol_checkpoints VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP(6))",
                    (dataset_id, row["symbol"], row["status"], row.get("error_class"), row.get("error_message")),
                )
            cursor.execute(
                """INSERT INTO m2_flow_runs VALUES
                (%s,%s,%s,%s,%s,%s,0,0,1,%s,%s,%s,CURRENT_TIMESTAMP(6))""",
                (dataset_id, manifest["schema_version"], manifest["business_date"], manifest["expected_symbol_count"],
                 manifest["available_symbol_count"], int(bool(manifest["data_available"])), manifest["facts_sha256"],
                 digest, json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
        connection.commit()
        return {"dataset_id": dataset_id, "idempotent_replay": False}
    except Exception:
        connection.rollback()
        raise


__all__ = ["TiDBConfig", "connect", "ensure_flow_schema", "publish_flow_run"]

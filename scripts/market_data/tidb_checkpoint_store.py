"""TiDB checkpoint storage for non-authoritative M2.3 market data.

This module is deliberately storage-only.  It does not fetch market data,
evaluate strategy signals, create simulated orders, or mark M2.3 as completed.
It stores accepted or explicitly checkpointed M2.3 evidence with deterministic
ids so interrupted cloud runs can be inspected and resumed without losing every
completed symbol.
"""

from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from scripts.market_data.manifest import sha256


TIDB_SCHEMA_VERSION = "m2-tidb-market-checkpoint-v3"
DEFAULT_ENV_FILE = Path(".env.local")


def _read_json(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _decimal_text(value: Any) -> str | None:
    return None if value is None else str(value)


def load_dotenv(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    """Load a simple KEY=value env file without printing secrets."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(frozen=True, slots=True)
class TiDBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    ssl_mode: str = "REQUIRED"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        env_file: Path = DEFAULT_ENV_FILE,
    ) -> "TiDBConfig":
        file_values = load_dotenv(env_file)
        source = {**file_values, **dict(os.environ if env is None else env)}
        missing = [
            key for key in ("TIDB_HOST", "TIDB_PORT", "TIDB_USER", "TIDB_PASSWORD", "TIDB_DATABASE")
            if not source.get(key)
        ]
        if missing:
            raise RuntimeError(f"missing TiDB configuration keys: {', '.join(missing)}")
        return cls(
            host=source["TIDB_HOST"],
            port=int(source["TIDB_PORT"]),
            user=source["TIDB_USER"],
            password=source["TIDB_PASSWORD"],
            database=source["TIDB_DATABASE"],
            ssl_mode=source.get("TIDB_SSL_MODE", "REQUIRED"),
        )

    def safe_summary(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "ssl_mode": self.ssl_mode,
            "password": "***",
        }


@dataclass(frozen=True, slots=True)
class HistoricalEvidence:
    manifest: dict[str, Any]
    bars: list[dict[str, Any]]
    tradeability: list[dict[str, Any]]
    adjustments: list[dict[str, Any]]
    references: list[dict[str, Any]]
    verification_checks: list[dict[str, Any]]


def load_historical_evidence(input_dir: Path) -> HistoricalEvidence:
    manifest_path = input_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing M2.3 manifest: {manifest_path}")
    verification_path = input_dir / "verification-checks.json.gz"
    return HistoricalEvidence(
        manifest=_read_json(manifest_path),
        bars=_read_json(input_dir / "historical-bars.json.gz"),
        tradeability=_read_json(input_dir / "tradeability.json.gz"),
        adjustments=_read_json(input_dir / "adjustment-events.json.gz"),
        references=_read_json(input_dir / "security-references.json"),
        verification_checks=_read_json(verification_path) if verification_path.exists() else [],
    )


def load_historical_manifest(input_dir: Path) -> HistoricalEvidence:
    """Load only a merged manifest when TiDB will reference physical shards.

    Full-universe merged evidence can contain millions of rows.  Manifest-only
    publication intentionally avoids loading or duplicating those rows after the
    merge process has already verified their deterministic hashes.
    """
    manifest_path = input_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing M2.3 manifest: {manifest_path}")
    return HistoricalEvidence(
        manifest=_read_json(manifest_path),
        bars=[],
        tradeability=[],
        adjustments=[],
        references=[],
        verification_checks=[],
    )


def default_dataset_id(manifest: Mapping[str, Any]) -> str:
    mode = str(manifest.get("mode", "unknown"))
    business_end = str(manifest.get("business_end", "unknown"))
    shard_count = manifest.get("shard_count")
    shard_index = manifest.get("shard_index")
    scope = "merged" if shard_index is None else f"shard-{shard_index}-of-{shard_count}"
    fingerprint = str(manifest.get("bars_sha256") or sha256(manifest))[:16]
    return f"m2-{mode}-{business_end}-{scope}-{fingerprint}"


def connect(config: TiDBConfig):
    try:
        import pymysql
    except ImportError as error:
        raise RuntimeError("PyMySQL is required for TiDB storage; install scripts/market_data/requirements.lock.txt") from error
    ssl = {"ssl": {"check_hostname": False}} if config.ssl_mode.upper() in {"REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"} else {}
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=20,
        read_timeout=60,
        write_timeout=60,
        **ssl,
    )


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS m2_history_runs (
      dataset_id VARCHAR(160) NOT NULL PRIMARY KEY,
      schema_version VARCHAR(64) NOT NULL,
      manifest_version VARCHAR(96) NULL,
      mode VARCHAR(32) NOT NULL,
      business_end DATE NULL,
      history_start DATE NULL,
      shard_index INT NULL,
      shard_count INT NULL,
      authoritative TINYINT NOT NULL,
      simulation_orders_allowed TINYINT NOT NULL,
      accepted TINYINT NOT NULL,
      global_symbol_count INT NULL,
      global_expected_key_count INT NULL,
      symbol_count INT NULL,
      expected_key_count INT NULL,
      bar_count INT NULL,
      tradeability_count INT NULL,
      adjustment_event_count INT NULL,
      reference_count INT NULL,
      verification_check_count INT NULL,
      bars_sha256 CHAR(64) NULL,
      tradeability_sha256 CHAR(64) NULL,
      adjustments_sha256 CHAR(64) NULL,
      references_sha256 CHAR(64) NULL,
      verification_checks_sha256 CHAR(64) NULL,
      manifest_sha256 CHAR(64) NOT NULL,
      manifest_json LONGTEXT NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      KEY idx_m2_history_runs_business_end (business_end),
      KEY idx_m2_history_runs_mode (mode)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_history_symbol_checkpoints (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      schema_version VARCHAR(64) NOT NULL,
      mode VARCHAR(32) NOT NULL,
      shard_index INT NULL,
      shard_count INT NULL,
      business_start DATE NULL,
      business_end DATE NULL,
      status VARCHAR(32) NOT NULL,
      accepted TINYINT NOT NULL,
      expected_rows INT NOT NULL,
      bar_rows INT NOT NULL,
      tradeability_rows INT NOT NULL,
      adjustment_rows INT NOT NULL,
      verification_rows INT NOT NULL,
      reference_present TINYINT NOT NULL,
      primary_source VARCHAR(96) NULL,
      bars_sha256 CHAR(64) NULL,
      tradeability_sha256 CHAR(64) NULL,
      adjustments_sha256 CHAR(64) NULL,
      error_class VARCHAR(128) NULL,
      error_message TEXT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol),
      KEY idx_m2_symbol_checkpoints_status (status),
      KEY idx_m2_symbol_checkpoints_business_end (business_end)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_historical_bars (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      exchange VARCHAR(16) NULL,
      index_code VARCHAR(32) NULL,
      open_price DECIMAL(18,4) NULL,
      high DECIMAL(18,4) NULL,
      low DECIMAL(18,4) NULL,
      close_price DECIMAL(18,4) NULL,
      previous_close DECIMAL(18,4) NULL,
      volume_shares BIGINT NULL,
      amount_cny DECIMAL(24,2) NULL,
      turnover_percent DECIMAL(18,6) NULL,
      qfq_factor DECIMAL(24,6) NULL,
      hfq_factor DECIMAL(24,6) NULL,
      qfq_open DECIMAL(18,4) NULL,
      qfq_high DECIMAL(18,4) NULL,
      qfq_low DECIMAL(18,4) NULL,
      qfq_close DECIMAL(18,4) NULL,
      hfq_open DECIMAL(18,4) NULL,
      hfq_high DECIMAL(18,4) NULL,
      hfq_low DECIMAL(18,4) NULL,
      hfq_close DECIMAL(18,4) NULL,
      primary_source VARCHAR(96) NULL,
      factor_source VARCHAR(96) NULL,
      source_schema_version VARCHAR(64) NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, business_date),
      KEY idx_m2_bars_business_date (business_date),
      KEY idx_m2_bars_symbol (symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_tradeability_facts (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      index_code VARCHAR(32) NULL,
      has_primary_bar TINYINT NOT NULL,
      has_secondary_status TINYINT NOT NULL,
      is_suspended TINYINT NOT NULL,
      is_st TINYINT NULL,
      listing_age_sessions INT NOT NULL,
      limit_rate DECIMAL(10,6) NULL,
      limit_up DECIMAL(18,4) NULL,
      limit_down DECIMAL(18,4) NULL,
      at_limit_up TINYINT NOT NULL,
      at_limit_down TINYINT NOT NULL,
      one_price_limit_up TINYINT NOT NULL,
      one_price_limit_down TINYINT NOT NULL,
      can_buy TINYINT NOT NULL,
      can_sell TINYINT NOT NULL,
      block_reasons_json LONGTEXT NOT NULL,
      source_schema_version VARCHAR(64) NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, business_date),
      KEY idx_m2_tradeability_business_date (business_date),
      KEY idx_m2_tradeability_can_buy (business_date, can_buy)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_history_verification_checks (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      primary_close DECIMAL(18,4) NOT NULL,
      verification_close DECIMAL(18,4) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, business_date),
      KEY idx_m2_verification_business_date (business_date),
      KEY idx_m2_verification_symbol (symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_adjustment_events (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      effective_date DATE NOT NULL,
      qfq_factor DECIMAL(24,6) NOT NULL,
      hfq_factor DECIMAL(24,6) NOT NULL,
      source VARCHAR(96) NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, effective_date),
      KEY idx_m2_adjustment_symbol (symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_security_references (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      exchange VARCHAR(16) NULL,
      name VARCHAR(255) NULL,
      ipo_date DATE NULL,
      out_date DATE NULL,
      source VARCHAR(96) NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol),
      KEY idx_m2_security_symbol (symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_history_run_shards (
      merged_dataset_id VARCHAR(160) NOT NULL,
      shard_dataset_id VARCHAR(160) NOT NULL,
      shard_index INT NOT NULL,
      shard_count INT NOT NULL,
      symbol_count INT NOT NULL,
      expected_key_count INT NOT NULL,
      bar_count INT NOT NULL,
      tradeability_count INT NOT NULL,
      adjustment_event_count INT NOT NULL,
      reference_count INT NOT NULL,
      verification_check_count INT NOT NULL,
      bars_sha256 CHAR(64) NOT NULL,
      tradeability_sha256 CHAR(64) NOT NULL,
      adjustments_sha256 CHAR(64) NOT NULL,
      references_sha256 CHAR(64) NOT NULL,
      verification_checks_sha256 CHAR(64) NOT NULL,
      shard_manifest_sha256 CHAR(64) NOT NULL,
      accepted TINYINT NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (merged_dataset_id, shard_index),
      UNIQUE KEY uq_m2_run_shards_dataset (merged_dataset_id, shard_dataset_id),
      KEY idx_m2_run_shards_physical_dataset (shard_dataset_id)
    )
    """,
)


def ensure_schema(connection: Any) -> None:
    with connection.cursor() as cursor:
        for statement in SCHEMA_STATEMENTS:
            cursor.execute(statement)
    connection.commit()


def _upsert_many(connection: Any, sql: str, rows: Iterable[tuple[Any, ...]]) -> int:
    values = list(rows)
    if not values:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(sql, values)
    return len(values)


RUN_UPSERT = """
INSERT INTO m2_history_runs (
  dataset_id, schema_version, manifest_version, mode, business_end, history_start,
  shard_index, shard_count, authoritative, simulation_orders_allowed, accepted,
  global_symbol_count, global_expected_key_count, symbol_count, expected_key_count,
  bar_count, tradeability_count, adjustment_event_count, reference_count,
  verification_check_count, bars_sha256, tradeability_sha256, adjustments_sha256,
  references_sha256, verification_checks_sha256, manifest_sha256, manifest_json
) VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON DUPLICATE KEY UPDATE
  schema_version=VALUES(schema_version), manifest_version=VALUES(manifest_version),
  mode=VALUES(mode), business_end=VALUES(business_end), history_start=VALUES(history_start),
  shard_index=VALUES(shard_index), shard_count=VALUES(shard_count),
  authoritative=VALUES(authoritative), simulation_orders_allowed=VALUES(simulation_orders_allowed),
  accepted=VALUES(accepted), global_symbol_count=VALUES(global_symbol_count),
  global_expected_key_count=VALUES(global_expected_key_count), symbol_count=VALUES(symbol_count),
  expected_key_count=VALUES(expected_key_count), bar_count=VALUES(bar_count),
  tradeability_count=VALUES(tradeability_count), adjustment_event_count=VALUES(adjustment_event_count),
  reference_count=VALUES(reference_count), verification_check_count=VALUES(verification_check_count),
  bars_sha256=VALUES(bars_sha256), tradeability_sha256=VALUES(tradeability_sha256),
  adjustments_sha256=VALUES(adjustments_sha256), references_sha256=VALUES(references_sha256),
  verification_checks_sha256=VALUES(verification_checks_sha256),
  manifest_sha256=VALUES(manifest_sha256), manifest_json=VALUES(manifest_json)
"""


BAR_UPSERT = """
INSERT INTO m2_historical_bars (
  dataset_id, symbol, business_date, exchange, index_code, open_price, high, low,
  close_price, previous_close, volume_shares, amount_cny, turnover_percent,
  qfq_factor, hfq_factor, qfq_open, qfq_high, qfq_low, qfq_close,
  hfq_open, hfq_high, hfq_low, hfq_close, primary_source, factor_source,
  source_schema_version, row_sha256
) VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON DUPLICATE KEY UPDATE
  exchange=VALUES(exchange), index_code=VALUES(index_code), open_price=VALUES(open_price),
  high=VALUES(high), low=VALUES(low), close_price=VALUES(close_price),
  previous_close=VALUES(previous_close), volume_shares=VALUES(volume_shares),
  amount_cny=VALUES(amount_cny), turnover_percent=VALUES(turnover_percent),
  qfq_factor=VALUES(qfq_factor), hfq_factor=VALUES(hfq_factor),
  qfq_open=VALUES(qfq_open), qfq_high=VALUES(qfq_high), qfq_low=VALUES(qfq_low),
  qfq_close=VALUES(qfq_close), hfq_open=VALUES(hfq_open), hfq_high=VALUES(hfq_high),
  hfq_low=VALUES(hfq_low), hfq_close=VALUES(hfq_close), primary_source=VALUES(primary_source),
  factor_source=VALUES(factor_source), source_schema_version=VALUES(source_schema_version),
  row_sha256=VALUES(row_sha256)
"""


TRADEABILITY_UPSERT = """
INSERT INTO m2_tradeability_facts (
  dataset_id, symbol, business_date, index_code, has_primary_bar, has_secondary_status,
  is_suspended, is_st, listing_age_sessions, limit_rate, limit_up, limit_down,
  at_limit_up, at_limit_down, one_price_limit_up, one_price_limit_down, can_buy,
  can_sell, block_reasons_json, source_schema_version, row_sha256
) VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON DUPLICATE KEY UPDATE
  index_code=VALUES(index_code), has_primary_bar=VALUES(has_primary_bar),
  has_secondary_status=VALUES(has_secondary_status), is_suspended=VALUES(is_suspended),
  is_st=VALUES(is_st), listing_age_sessions=VALUES(listing_age_sessions),
  limit_rate=VALUES(limit_rate), limit_up=VALUES(limit_up), limit_down=VALUES(limit_down),
  at_limit_up=VALUES(at_limit_up), at_limit_down=VALUES(at_limit_down),
  one_price_limit_up=VALUES(one_price_limit_up), one_price_limit_down=VALUES(one_price_limit_down),
  can_buy=VALUES(can_buy), can_sell=VALUES(can_sell),
  block_reasons_json=VALUES(block_reasons_json), source_schema_version=VALUES(source_schema_version),
  row_sha256=VALUES(row_sha256)
"""


ADJUSTMENT_UPSERT = """
INSERT INTO m2_adjustment_events (
  dataset_id, symbol, effective_date, qfq_factor, hfq_factor, source, row_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  qfq_factor=VALUES(qfq_factor), hfq_factor=VALUES(hfq_factor),
  source=VALUES(source), row_sha256=VALUES(row_sha256)
"""


REFERENCE_UPSERT = """
INSERT INTO m2_security_references (
  dataset_id, symbol, exchange, name, ipo_date, out_date, source, row_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  exchange=VALUES(exchange), name=VALUES(name), ipo_date=VALUES(ipo_date),
  out_date=VALUES(out_date), source=VALUES(source), row_sha256=VALUES(row_sha256)
"""


VERIFICATION_UPSERT = """
INSERT INTO m2_history_verification_checks (
  dataset_id, symbol, business_date, primary_close, verification_close, row_sha256
) VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  primary_close=VALUES(primary_close), verification_close=VALUES(verification_close),
  row_sha256=VALUES(row_sha256)
"""


CHECKPOINT_UPSERT = """
INSERT INTO m2_history_symbol_checkpoints (
  dataset_id, symbol, schema_version, mode, shard_index, shard_count, business_start,
  business_end, status, accepted, expected_rows, bar_rows, tradeability_rows,
  adjustment_rows, verification_rows, reference_present, primary_source,
  bars_sha256, tradeability_sha256, adjustments_sha256, error_class, error_message
) VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON DUPLICATE KEY UPDATE
  schema_version=VALUES(schema_version), mode=VALUES(mode), shard_index=VALUES(shard_index),
  shard_count=VALUES(shard_count), business_start=VALUES(business_start),
  business_end=VALUES(business_end), status=VALUES(status), accepted=VALUES(accepted),
  expected_rows=VALUES(expected_rows), bar_rows=VALUES(bar_rows),
  tradeability_rows=VALUES(tradeability_rows), adjustment_rows=VALUES(adjustment_rows),
  verification_rows=VALUES(verification_rows), reference_present=VALUES(reference_present),
  primary_source=VALUES(primary_source), bars_sha256=VALUES(bars_sha256),
  tradeability_sha256=VALUES(tradeability_sha256), adjustments_sha256=VALUES(adjustments_sha256),
  error_class=VALUES(error_class), error_message=VALUES(error_message)
"""


RUN_SHARD_UPSERT = """
INSERT INTO m2_history_run_shards (
  merged_dataset_id, shard_dataset_id, shard_index, shard_count, symbol_count,
  expected_key_count, bar_count, tradeability_count, adjustment_event_count,
  reference_count, verification_check_count, bars_sha256, tradeability_sha256,
  adjustments_sha256, references_sha256, verification_checks_sha256,
  shard_manifest_sha256, accepted
) VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON DUPLICATE KEY UPDATE
  shard_dataset_id=VALUES(shard_dataset_id), shard_count=VALUES(shard_count),
  symbol_count=VALUES(symbol_count), expected_key_count=VALUES(expected_key_count),
  bar_count=VALUES(bar_count), tradeability_count=VALUES(tradeability_count),
  adjustment_event_count=VALUES(adjustment_event_count), reference_count=VALUES(reference_count),
  verification_check_count=VALUES(verification_check_count), bars_sha256=VALUES(bars_sha256),
  tradeability_sha256=VALUES(tradeability_sha256), adjustments_sha256=VALUES(adjustments_sha256),
  references_sha256=VALUES(references_sha256),
  verification_checks_sha256=VALUES(verification_checks_sha256),
  shard_manifest_sha256=VALUES(shard_manifest_sha256), accepted=VALUES(accepted)
"""


def _run_row(
    dataset_id: str,
    evidence: HistoricalEvidence,
    *,
    logical_manifest_counts: bool = False,
) -> tuple[Any, ...]:
    manifest = evidence.manifest
    manifest_json = _compact_json(manifest)
    shards = manifest.get("shard_manifests")
    first_shard = shards[0] if isinstance(shards, list) and shards else {}
    if logical_manifest_counts:
        bar_count = _optional_int(manifest.get("bar_count"))
        tradeability_count = _optional_int(manifest.get("tradeability_count"))
        adjustment_count = _optional_int(manifest.get("adjustment_event_count"))
        reference_count = _optional_int(manifest.get("reference_count"))
        verification_count = _optional_int(manifest.get("verification_check_count"))
    else:
        bar_count = len(evidence.bars)
        tradeability_count = len(evidence.tradeability)
        adjustment_count = len(evidence.adjustments)
        reference_count = len(evidence.references)
        verification_count = len(evidence.verification_checks)
    return (
        dataset_id,
        TIDB_SCHEMA_VERSION,
        _optional_text(manifest.get("manifest_version")),
        _optional_text(manifest.get("mode")) or "unknown",
        _optional_text(manifest.get("business_end")),
        _optional_text(manifest.get("history_start") or first_shard.get("history_start")),
        _optional_int(manifest.get("shard_index")),
        _optional_int(manifest.get("shard_count")),
        1 if bool(manifest.get("authoritative")) else 0,
        1 if bool(manifest.get("simulation_orders_allowed")) else 0,
        1 if bool(manifest.get("accepted")) else 0,
        _optional_int(manifest.get("global_symbol_count")),
        _optional_int(manifest.get("global_expected_key_count")),
        _optional_int(manifest.get("symbol_count") or manifest.get("global_symbol_count")),
        _optional_int(manifest.get("expected_key_count") or manifest.get("global_expected_key_count")),
        bar_count,
        tradeability_count,
        adjustment_count,
        reference_count,
        verification_count,
        _optional_text(manifest.get("bars_sha256")),
        _optional_text(manifest.get("tradeability_sha256")),
        _optional_text(manifest.get("adjustments_sha256")),
        _optional_text(manifest.get("references_sha256")),
        _optional_text(manifest.get("verification_checks_sha256")),
        sha256(manifest),
        manifest_json,
    )


def _required_nonnegative_int(value: Mapping[str, Any], key: str, scope: str) -> int:
    if key not in value or isinstance(value[key], bool):
        raise RuntimeError(f"{scope} is missing integer {key}")
    parsed = int(value[key])
    if parsed < 0:
        raise RuntimeError(f"{scope} has negative {key}")
    return parsed


def _required_sha256(value: Mapping[str, Any], key: str, scope: str) -> str:
    digest = str(value.get(key, ""))
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
        raise RuntimeError(f"{scope} has invalid {key}")
    return digest.lower()


def merged_shard_rows(dataset_id: str, manifest: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    """Validate and map an accepted logical merge to its physical TiDB shards."""
    if not str(manifest.get("manifest_version", "")).startswith("m2-historical-market-merged-manifest-"):
        raise RuntimeError("manifest-only publication requires a merged M2.3 manifest")
    if manifest.get("shard_index") is not None:
        raise RuntimeError("manifest-only publication refuses a physical shard manifest")
    if manifest.get("accepted") is not True:
        raise RuntimeError("manifest-only publication requires an accepted merged manifest")

    required_merge_checks = (
        "consistent_shard_metadata",
        "complete_shard_inventory",
        "all_shards_accepted",
        "shard_content_hashes_reconcile",
        "no_cross_shard_duplicates",
        "expected_counts_reconcile",
        "global_aggregate_accepted",
        "verification_counts_reconcile",
        "verification_symbol_inventory_reconciles",
        "cross_source_accepted",
    )
    merge_checks = manifest.get("merge_checks")
    if not isinstance(merge_checks, Mapping) or not all(merge_checks.get(key) is True for key in required_merge_checks):
        raise RuntimeError("manifest-only publication requires every critical merge check to pass")

    shards = manifest.get("shard_manifests")
    if not isinstance(shards, list) or not shards:
        raise RuntimeError("manifest-only publication requires shard_manifests")
    shard_count = _required_nonnegative_int(manifest, "shard_count", "merged manifest")
    if shard_count < 1 or len(shards) != shard_count:
        raise RuntimeError("merged shard inventory count does not reconcile")

    expected_indices = list(range(shard_count))
    actual_indices = sorted(_required_nonnegative_int(shard, "shard_index", "shard manifest") for shard in shards)
    if actual_indices != expected_indices:
        raise RuntimeError("merged shard indices are incomplete or duplicated")

    global_symbols = _required_nonnegative_int(manifest, "global_symbol_count", "merged manifest")
    global_expected = _required_nonnegative_int(manifest, "global_expected_key_count", "merged manifest")
    logical_totals = {
        "symbol_count": global_symbols,
        "expected_key_count": global_expected,
        "bar_count": _required_nonnegative_int(manifest, "bar_count", "merged manifest"),
        "tradeability_count": _required_nonnegative_int(manifest, "tradeability_count", "merged manifest"),
        "adjustment_event_count": _required_nonnegative_int(manifest, "adjustment_event_count", "merged manifest"),
        "reference_count": _required_nonnegative_int(manifest, "reference_count", "merged manifest"),
        "verification_check_count": _required_nonnegative_int(manifest, "verification_check_count", "merged manifest"),
    }
    for hash_key in (
        "bars_sha256", "tradeability_sha256", "adjustments_sha256",
        "references_sha256", "verification_checks_sha256",
    ):
        _required_sha256(manifest, hash_key, "merged manifest")

    rows: list[tuple[Any, ...]] = []
    dataset_ids: set[str] = set()
    totals = {key: 0 for key in logical_totals}
    for shard in sorted(shards, key=lambda item: int(item["shard_index"])):
        scope = f"shard {shard['shard_index']}"
        if shard.get("accepted") is not True:
            raise RuntimeError(f"{scope} is not accepted")
        if shard.get("authoritative") is not False or shard.get("simulation_orders_allowed") is not False:
            raise RuntimeError(f"{scope} violates the non-authoritative simulation-only boundary")
        if (
            shard.get("mode") != manifest.get("mode")
            or shard.get("business_end") != manifest.get("business_end")
            or _required_nonnegative_int(shard, "shard_count", scope) != shard_count
            or _required_nonnegative_int(shard, "global_symbol_count", scope) != global_symbols
            or _required_nonnegative_int(shard, "global_expected_key_count", scope) != global_expected
        ):
            raise RuntimeError(f"{scope} metadata does not match the merged manifest")
        shard_dataset_id = str(shard.get("checkpoint_dataset_id", "")).strip()
        if not shard_dataset_id or len(shard_dataset_id) > 160 or shard_dataset_id in dataset_ids:
            raise RuntimeError(f"{scope} has a missing, oversized, or duplicate checkpoint_dataset_id")
        dataset_ids.add(shard_dataset_id)
        counts = {
            key: _required_nonnegative_int(shard, key, scope)
            for key in logical_totals
        }
        for key, count in counts.items():
            totals[key] += count
        hashes = {
            key: _required_sha256(shard, key, scope)
            for key in (
                "bars_sha256", "tradeability_sha256", "adjustments_sha256",
                "references_sha256", "verification_checks_sha256",
            )
        }
        rows.append((
            dataset_id,
            shard_dataset_id,
            int(shard["shard_index"]),
            shard_count,
            counts["symbol_count"],
            counts["expected_key_count"],
            counts["bar_count"],
            counts["tradeability_count"],
            counts["adjustment_event_count"],
            counts["reference_count"],
            counts["verification_check_count"],
            hashes["bars_sha256"],
            hashes["tradeability_sha256"],
            hashes["adjustments_sha256"],
            hashes["references_sha256"],
            hashes["verification_checks_sha256"],
            sha256(shard),
            1,
        ))
    if totals != logical_totals:
        differences = {key: {"merged": logical_totals[key], "shards": totals[key]} for key in totals if totals[key] != logical_totals[key]}
        raise RuntimeError(f"merged logical counts do not reconcile to physical shards: {_compact_json(differences)}")
    return rows


def validate_physical_shards(
    connection: Any,
    manifest: Mapping[str, Any],
    mapping_rows: list[tuple[Any, ...]],
) -> None:
    """Fail closed unless every logical pointer matches a committed shard run."""
    shard_manifests = {
        str(shard["checkpoint_dataset_id"]): shard
        for shard in manifest["shard_manifests"]
    }
    dataset_ids = [str(row[1]) for row in mapping_rows]
    placeholders = ", ".join(["%s"] * len(dataset_ids))
    sql = f"""
        SELECT dataset_id, mode, business_end, shard_index, shard_count,
               authoritative, simulation_orders_allowed, accepted,
               global_symbol_count, global_expected_key_count, symbol_count,
               expected_key_count, bar_count, tradeability_count,
               adjustment_event_count, reference_count, verification_check_count,
               bars_sha256, tradeability_sha256, adjustments_sha256,
               references_sha256, verification_checks_sha256, manifest_sha256
        FROM m2_history_runs
        WHERE dataset_id IN ({placeholders})
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(dataset_ids))
        stored_rows = list(cursor.fetchall())
    stored_by_id = {str(row[0]): row for row in stored_rows}
    if len(stored_by_id) != len(dataset_ids):
        missing = sorted(set(dataset_ids) - set(stored_by_id))
        raise RuntimeError(f"physical TiDB shard runs are missing: {', '.join(missing)}")

    for dataset_id in dataset_ids:
        shard = shard_manifests[dataset_id]
        stored = stored_by_id[dataset_id]
        expected = (
            str(shard["mode"]),
            str(shard["business_end"]),
            int(shard["shard_index"]),
            int(shard["shard_count"]),
            0,
            0,
            1,
            int(shard["global_symbol_count"]),
            int(shard["global_expected_key_count"]),
            int(shard["symbol_count"]),
            int(shard["expected_key_count"]),
            int(shard["bar_count"]),
            int(shard["tradeability_count"]),
            int(shard["adjustment_event_count"]),
            int(shard["reference_count"]),
            int(shard["verification_check_count"]),
            str(shard["bars_sha256"]).lower(),
            str(shard["tradeability_sha256"]).lower(),
            str(shard["adjustments_sha256"]).lower(),
            str(shard["references_sha256"]).lower(),
            str(shard["verification_checks_sha256"]).lower(),
            sha256(shard),
        )
        actual = (
            str(stored[1]),
            _date_text(stored[2]),
            int(stored[3]),
            int(stored[4]),
            int(stored[5]),
            int(stored[6]),
            int(stored[7]),
            int(stored[8]),
            int(stored[9]),
            int(stored[10]),
            int(stored[11]),
            int(stored[12]),
            int(stored[13]),
            int(stored[14]),
            int(stored[15]),
            int(stored[16]),
            str(stored[17]).lower(),
            str(stored[18]).lower(),
            str(stored[19]).lower(),
            str(stored[20]).lower(),
            str(stored[21]).lower(),
            str(stored[22]).lower(),
        )
        if actual != expected:
            raise RuntimeError(f"physical TiDB shard run does not match merged evidence: {dataset_id}")


def _bar_rows(dataset_id: str, bars: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            dataset_id, row["symbol"], row["business_date"], row.get("exchange"), row.get("index_code"),
            _decimal_text(row.get("open")), _decimal_text(row.get("high")), _decimal_text(row.get("low")),
            _decimal_text(row.get("close")), _decimal_text(row.get("previous_close")),
            _optional_int(row.get("volume_shares")), _decimal_text(row.get("amount_cny")),
            _decimal_text(row.get("turnover_percent")), _decimal_text(row.get("qfq_factor")),
            _decimal_text(row.get("hfq_factor")), _decimal_text(row.get("qfq_open")),
            _decimal_text(row.get("qfq_high")), _decimal_text(row.get("qfq_low")),
            _decimal_text(row.get("qfq_close")), _decimal_text(row.get("hfq_open")),
            _decimal_text(row.get("hfq_high")), _decimal_text(row.get("hfq_low")),
            _decimal_text(row.get("hfq_close")), row.get("primary_source"),
            row.get("factor_source"), row.get("schema_version"), sha256(row),
        )
        for row in bars
    ]


def _tradeability_rows(dataset_id: str, facts: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            dataset_id, row["symbol"], row["business_date"], row.get("index_code"),
            _optional_bool(row.get("has_primary_bar")), _optional_bool(row.get("has_secondary_status")),
            _optional_bool(row.get("is_suspended")), _optional_bool(row.get("is_st")),
            _optional_int(row.get("listing_age_sessions")), _decimal_text(row.get("limit_rate")),
            _decimal_text(row.get("limit_up")), _decimal_text(row.get("limit_down")),
            _optional_bool(row.get("at_limit_up")), _optional_bool(row.get("at_limit_down")),
            _optional_bool(row.get("one_price_limit_up")), _optional_bool(row.get("one_price_limit_down")),
            _optional_bool(row.get("can_buy")), _optional_bool(row.get("can_sell")),
            _compact_json(row.get("block_reasons", [])), row.get("schema_version"), sha256(row),
        )
        for row in facts
    ]


def _adjustment_rows(dataset_id: str, events: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            dataset_id, row["symbol"], row["effective_date"],
            _decimal_text(row.get("qfq_factor")), _decimal_text(row.get("hfq_factor")),
            row.get("source"), sha256(row),
        )
        for row in events
    ]


def _reference_rows(dataset_id: str, references: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            dataset_id, row["symbol"], row.get("exchange"), row.get("name"),
            row.get("ipo_date"), row.get("out_date"), row.get("source"), sha256(row),
        )
        for row in references
    ]


def _verification_rows(dataset_id: str, checks: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            dataset_id, row["symbol"], row["business_date"],
            _decimal_text(row.get("primary_close")), _decimal_text(row.get("verification_close")),
            sha256(row),
        )
        for row in checks
    ]


def _by_symbol(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    output: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        output[str(row["symbol"])].append(row)
    return dict(output)


def _date_bounds(rows: Iterable[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    dates = sorted(str(row["business_date"]) for row in rows if row.get("business_date"))
    if not dates:
        return None, None
    return dates[0], dates[-1]


def symbol_checkpoint_rows(dataset_id: str, evidence: HistoricalEvidence) -> list[tuple[Any, ...]]:
    manifest = evidence.manifest
    bars = _by_symbol(evidence.bars)
    facts = _by_symbol(evidence.tradeability)
    adjustments = _by_symbol(evidence.adjustments)
    references = {str(row["symbol"]): row for row in evidence.references}
    verifications = _by_symbol(evidence.verification_checks)
    primary_failures = {str(key): str(value) for key, value in manifest.get("primary_failures", {}).items()}
    primary_sources = {str(key): str(value) for key, value in manifest.get("primary_sources_by_symbol", {}).items()}
    symbols = sorted(set(bars) | set(facts) | set(adjustments) | set(references) | set(verifications) | set(primary_failures))
    rows: list[tuple[Any, ...]] = []
    for symbol in symbols:
        symbol_bars = bars.get(symbol, [])
        symbol_facts = facts.get(symbol, [])
        symbol_adjustments = adjustments.get(symbol, [])
        start, end = _date_bounds(symbol_facts or symbol_bars)
        error_message = primary_failures.get(symbol)
        status = "failed" if error_message or not symbol_bars else "succeeded"
        accepted = status == "succeeded" and bool(manifest.get("accepted"))
        rows.append((
            dataset_id,
            symbol,
            TIDB_SCHEMA_VERSION,
            str(manifest.get("mode", "unknown")),
            _optional_int(manifest.get("shard_index")),
            _optional_int(manifest.get("shard_count")),
            start,
            end or _optional_text(manifest.get("business_end")),
            status,
            1 if accepted else 0,
            len(symbol_facts),
            len(symbol_bars),
            len(symbol_facts),
            len(symbol_adjustments),
            len(verifications.get(symbol, [])),
            1 if symbol in references else 0,
            primary_sources.get(symbol),
            sha256(symbol_bars) if symbol_bars else None,
            sha256(symbol_facts) if symbol_facts else None,
            sha256(symbol_adjustments) if symbol_adjustments else None,
            "primary_failure" if error_message else ("missing_bars" if not symbol_bars else None),
            error_message,
        ))
    return rows


def publish_historical_evidence(
    connection: Any,
    evidence: HistoricalEvidence,
    *,
    dataset_id: str | None = None,
    allow_unaccepted_checkpoint: bool = False,
    manifest_only: bool = False,
) -> dict[str, Any]:
    manifest = evidence.manifest
    if manifest.get("simulation_orders_allowed") is not False:
        raise RuntimeError("refusing to publish evidence that is not explicitly simulation_orders_allowed=false")
    if manifest.get("authoritative") is not False:
        raise RuntimeError("refusing to publish evidence that is not explicitly authoritative=false")
    if not manifest.get("accepted") and not allow_unaccepted_checkpoint:
        raise RuntimeError("refusing to publish unaccepted evidence without --allow-unaccepted-checkpoint")
    resolved_dataset_id = dataset_id or default_dataset_id(manifest)
    if manifest_only and allow_unaccepted_checkpoint:
        raise RuntimeError("manifest-only publication cannot allow an unaccepted checkpoint")
    shard_rows = merged_shard_rows(resolved_dataset_id, manifest) if manifest_only else []
    if manifest_only:
        validate_physical_shards(connection, manifest, shard_rows)
    with connection.cursor() as cursor:
        cursor.execute(
            RUN_UPSERT,
            _run_row(resolved_dataset_id, evidence, logical_manifest_counts=manifest_only),
        )
    counts = {
        "runs": 1,
        "shard_mappings": _upsert_many(connection, RUN_SHARD_UPSERT, shard_rows),
        "bars": 0 if manifest_only else _upsert_many(connection, BAR_UPSERT, _bar_rows(resolved_dataset_id, evidence.bars)),
        "tradeability": 0 if manifest_only else _upsert_many(connection, TRADEABILITY_UPSERT, _tradeability_rows(resolved_dataset_id, evidence.tradeability)),
        "adjustments": 0 if manifest_only else _upsert_many(connection, ADJUSTMENT_UPSERT, _adjustment_rows(resolved_dataset_id, evidence.adjustments)),
        "references": 0 if manifest_only else _upsert_many(connection, REFERENCE_UPSERT, _reference_rows(resolved_dataset_id, evidence.references)),
        "verification_checks": 0 if manifest_only else _upsert_many(connection, VERIFICATION_UPSERT, _verification_rows(resolved_dataset_id, evidence.verification_checks)),
        "symbol_checkpoints": 0 if manifest_only else _upsert_many(connection, CHECKPOINT_UPSERT, symbol_checkpoint_rows(resolved_dataset_id, evidence)),
    }
    connection.commit()
    return {
        "dataset_id": resolved_dataset_id,
        "schema_version": TIDB_SCHEMA_VERSION,
        "accepted": bool(manifest.get("accepted")),
        "mode": manifest.get("mode"),
        "business_end": manifest.get("business_end"),
        "storage_mode": "manifest_only_shards" if manifest_only else "physical_rows",
        "counts": counts,
        "simulation_orders_allowed": False,
    }


def publish_symbol_checkpoint(
    connection: Any,
    evidence: HistoricalEvidence,
    *,
    dataset_id: str,
) -> dict[str, int]:
    """Atomically persist exactly one fail-closed symbol capture.

    The aggregate run row is intentionally not written here.  A cloud runner may
    disappear between symbols; the final artifact publisher writes the aggregate
    manifest only after it exists, while these rows remain resumable checkpoints.
    """
    manifest = evidence.manifest
    if manifest.get("simulation_orders_allowed") is not False:
        raise RuntimeError("refusing symbol checkpoint without simulation_orders_allowed=false")
    if manifest.get("authoritative") is not False:
        raise RuntimeError("refusing authoritative symbol checkpoint")
    checkpoints = symbol_checkpoint_rows(dataset_id, evidence)
    if len(checkpoints) != 1:
        raise ValueError(f"symbol checkpoint requires exactly one symbol, got {len(checkpoints)}")
    counts = {
        "bars": _upsert_many(connection, BAR_UPSERT, _bar_rows(dataset_id, evidence.bars)),
        "tradeability": _upsert_many(connection, TRADEABILITY_UPSERT, _tradeability_rows(dataset_id, evidence.tradeability)),
        "adjustments": _upsert_many(connection, ADJUSTMENT_UPSERT, _adjustment_rows(dataset_id, evidence.adjustments)),
        "references": _upsert_many(connection, REFERENCE_UPSERT, _reference_rows(dataset_id, evidence.references)),
        "verification_checks": _upsert_many(connection, VERIFICATION_UPSERT, _verification_rows(dataset_id, evidence.verification_checks)),
        "symbol_checkpoints": _upsert_many(connection, CHECKPOINT_UPSERT, checkpoints),
    }
    connection.commit()
    return counts


def _query_all(connection: Any, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _number_text(value: Any) -> str | None:
    return None if value is None else format(value, "f")


def load_resumable_evidence(connection: Any, dataset_id: str) -> HistoricalEvidence:
    """Rebuild canonical evidence for successfully captured symbols only."""
    succeeded_rows = _query_all(
        connection,
        "SELECT symbol FROM m2_history_symbol_checkpoints WHERE dataset_id=%s AND status='succeeded' ORDER BY symbol",
        (dataset_id,),
    )
    succeeded = {str(row[0]) for row in succeeded_rows}
    if not succeeded:
        return HistoricalEvidence(
            manifest={"dataset_id": dataset_id, "resumed_symbols": []},
            bars=[], tradeability=[], adjustments=[], references=[], verification_checks=[],
        )

    def retained(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [row for row in rows if str(row[0]) in succeeded]

    bars = retained(_query_all(connection, """
        SELECT symbol, business_date, exchange, index_code, open_price, high, low, close_price,
               previous_close, volume_shares, amount_cny, turnover_percent, qfq_factor, hfq_factor,
               qfq_open, qfq_high, qfq_low, qfq_close, hfq_open, hfq_high, hfq_low, hfq_close,
               primary_source, factor_source, source_schema_version
        FROM m2_historical_bars WHERE dataset_id=%s ORDER BY symbol, business_date
    """, (dataset_id,)))
    facts = retained(_query_all(connection, """
        SELECT symbol, business_date, index_code, has_primary_bar, has_secondary_status,
               is_suspended, is_st, listing_age_sessions, limit_rate, limit_up, limit_down,
               at_limit_up, at_limit_down, one_price_limit_up, one_price_limit_down,
               can_buy, can_sell, block_reasons_json, source_schema_version
        FROM m2_tradeability_facts WHERE dataset_id=%s ORDER BY symbol, business_date
    """, (dataset_id,)))
    adjustments = retained(_query_all(connection, """
        SELECT symbol, effective_date, qfq_factor, hfq_factor, source
        FROM m2_adjustment_events WHERE dataset_id=%s ORDER BY symbol, effective_date
    """, (dataset_id,)))
    references = retained(_query_all(connection, """
        SELECT symbol, exchange, name, ipo_date, out_date, source
        FROM m2_security_references WHERE dataset_id=%s ORDER BY symbol
    """, (dataset_id,)))
    checks = retained(_query_all(connection, """
        SELECT symbol, business_date, primary_close, verification_close
        FROM m2_history_verification_checks WHERE dataset_id=%s ORDER BY symbol, business_date
    """, (dataset_id,)))
    return HistoricalEvidence(
        manifest={"dataset_id": dataset_id, "resumed_symbols": sorted(succeeded)},
        bars=[{
            "symbol": str(row[0]), "business_date": _date_text(row[1]), "exchange": row[2],
            "index_code": row[3], "open": _number_text(row[4]), "high": _number_text(row[5]),
            "low": _number_text(row[6]), "close": _number_text(row[7]),
            "previous_close": _number_text(row[8]), "volume_shares": int(row[9]),
            "amount_cny": _number_text(row[10]), "turnover_percent": _number_text(row[11]),
            "qfq_factor": _number_text(row[12]), "hfq_factor": _number_text(row[13]),
            "qfq_open": _number_text(row[14]), "qfq_high": _number_text(row[15]),
            "qfq_low": _number_text(row[16]), "qfq_close": _number_text(row[17]),
            "hfq_open": _number_text(row[18]), "hfq_high": _number_text(row[19]),
            "hfq_low": _number_text(row[20]), "hfq_close": _number_text(row[21]),
            "primary_source": row[22], "factor_source": row[23], "schema_version": row[24],
        } for row in bars],
        tradeability=[{
            "symbol": str(row[0]), "business_date": _date_text(row[1]), "index_code": row[2],
            "has_primary_bar": bool(row[3]), "has_secondary_status": bool(row[4]),
            "is_suspended": bool(row[5]), "is_st": None if row[6] is None else bool(row[6]),
            "listing_age_sessions": int(row[7]), "limit_rate": _number_text(row[8]),
            "limit_up": _number_text(row[9]), "limit_down": _number_text(row[10]),
            "at_limit_up": bool(row[11]), "at_limit_down": bool(row[12]),
            "one_price_limit_up": bool(row[13]), "one_price_limit_down": bool(row[14]),
            "can_buy": bool(row[15]), "can_sell": bool(row[16]),
            "block_reasons": json.loads(row[17]), "schema_version": row[18],
        } for row in facts],
        adjustments=[{
            "symbol": str(row[0]), "effective_date": _date_text(row[1]),
            "qfq_factor": _number_text(row[2]), "hfq_factor": _number_text(row[3]), "source": row[4],
        } for row in adjustments],
        references=[{
            "symbol": str(row[0]), "exchange": row[1], "name": row[2],
            "ipo_date": _date_text(row[3]), "out_date": _date_text(row[4]), "source": row[5],
        } for row in references],
        verification_checks=[{
            "symbol": str(row[0]), "business_date": _date_text(row[1]),
            "primary_close": _number_text(row[2]), "verification_close": _number_text(row[3]),
        } for row in checks],
    )

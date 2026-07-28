"""TiDB checkpoints and atomic publication for M2 daily market increments.

Partial symbol rows are resumable research checkpoints.  They become visible to
future consumers only after one accepted aggregate run is published.  All data
remains non-authoritative and cannot create simulated orders by itself.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from scripts.market_data.daily_adjustments import PreviousAdjustedState
from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect


DAILY_STORE_SCHEMA_VERSION = "m2-tidb-daily-checkpoint-v1"


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal(value: Any) -> str | None:
    return None if value is None else format(value, "f")


def _bool(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _date_text(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _read_gzip_json(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"invalid daily evidence file: {path}")
    return value


@dataclass(frozen=True, slots=True)
class DailyEvidence:
    manifest: dict[str, Any]
    primary_bars: list[dict[str, Any]]
    tradeability: list[dict[str, Any]]
    verification_bars: list[dict[str, Any]]
    adjusted_bars: list[dict[str, Any]]
    adjustments: list[dict[str, Any]]


def load_daily_evidence(input_dir: Path) -> DailyEvidence:
    manifest_path = input_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("daily manifest must be an object")
    return DailyEvidence(
        manifest=manifest,
        primary_bars=_read_gzip_json(input_dir / "daily-primary-bars.json.gz"),
        tradeability=_read_gzip_json(input_dir / "daily-tradeability.json.gz"),
        verification_bars=_read_gzip_json(input_dir / "daily-verification-bars.json.gz"),
        adjusted_bars=_read_gzip_json(input_dir / "daily-adjusted-bars.json.gz"),
        adjustments=_read_gzip_json(input_dir / "daily-adjustment-events.json.gz"),
    )


def default_daily_dataset_id(target_session: date | str, scope_sha256: str) -> str:
    target = target_session.isoformat() if isinstance(target_session, date) else str(target_session)
    if len(scope_sha256) != 64 or any(value not in "0123456789abcdef" for value in scope_sha256):
        raise ValueError("daily dataset id requires a lowercase SHA-256 scope")
    return f"m2-daily-{target}-{scope_sha256}"


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS m2_daily_runs (
      dataset_id VARCHAR(160) NOT NULL PRIMARY KEY,
      schema_version VARCHAR(64) NOT NULL,
      manifest_version VARCHAR(96) NOT NULL,
      target_session DATE NOT NULL,
      previous_session DATE NOT NULL,
      snapshot_effective_session DATE NOT NULL,
      base_history_dataset_id VARCHAR(160) NOT NULL,
      predecessor_dataset_id VARCHAR(160) NOT NULL,
      scope_sha256 CHAR(64) NOT NULL,
      authoritative TINYINT NOT NULL,
      simulation_orders_allowed TINYINT NOT NULL,
      accepted TINYINT NOT NULL,
      expected_symbol_count INT NOT NULL,
      primary_row_count INT NOT NULL,
      adjusted_row_count INT NOT NULL,
      tradeability_row_count INT NOT NULL,
      verification_row_count INT NOT NULL,
      adjustment_event_count INT NOT NULL,
      primary_sha256 CHAR(64) NOT NULL,
      adjusted_sha256 CHAR(64) NOT NULL,
      tradeability_sha256 CHAR(64) NOT NULL,
      verification_sha256 CHAR(64) NOT NULL,
      adjustments_sha256 CHAR(64) NOT NULL,
      quality_sha256 CHAR(64) NOT NULL,
      manifest_sha256 CHAR(64) NOT NULL,
      manifest_json LONGTEXT NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      UNIQUE KEY uq_m2_daily_target_scope (target_session, scope_sha256),
      KEY idx_m2_daily_runs_target (target_session, accepted),
      KEY idx_m2_daily_runs_base (base_history_dataset_id, target_session)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_daily_symbol_checkpoints (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      target_session DATE NOT NULL,
      status VARCHAR(32) NOT NULL,
      primary_present TINYINT NOT NULL,
      adjusted_present TINYINT NOT NULL,
      tradeability_present TINYINT NOT NULL,
      verification_required TINYINT NOT NULL,
      verification_present TINYINT NOT NULL,
      reported_previous_close DECIMAL(18,4) NULL,
      primary_sha256 CHAR(64) NULL,
      adjusted_sha256 CHAR(64) NULL,
      tradeability_sha256 CHAR(64) NULL,
      verification_sha256 CHAR(64) NULL,
      adjustments_sha256 CHAR(64) NULL,
      error_class VARCHAR(128) NULL,
      error_message TEXT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol),
      KEY idx_m2_daily_checkpoints_status (dataset_id, status)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_daily_primary_bars (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      source VARCHAR(96) NOT NULL,
      exchange VARCHAR(16) NOT NULL,
      open_price DECIMAL(18,4) NOT NULL,
      high DECIMAL(18,4) NOT NULL,
      low DECIMAL(18,4) NOT NULL,
      close_price DECIMAL(18,4) NOT NULL,
      previous_close DECIMAL(18,4) NULL,
      volume_shares BIGINT NOT NULL,
      amount_cny DECIMAL(24,2) NOT NULL,
      turnover_percent DECIMAL(18,6) NULL,
      trade_status VARCHAR(64) NOT NULL,
      is_st TINYINT NULL,
      adjustment VARCHAR(32) NOT NULL,
      source_schema_version VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, business_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_daily_adjusted_bars (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      exchange VARCHAR(16) NOT NULL,
      index_code VARCHAR(32) NOT NULL,
      open_price DECIMAL(18,4) NOT NULL,
      high DECIMAL(18,4) NOT NULL,
      low DECIMAL(18,4) NOT NULL,
      close_price DECIMAL(18,4) NOT NULL,
      previous_close DECIMAL(18,4) NOT NULL,
      volume_shares BIGINT NOT NULL,
      amount_cny DECIMAL(24,2) NOT NULL,
      turnover_percent DECIMAL(18,6) NULL,
      qfq_factor DECIMAL(24,6) NOT NULL,
      hfq_factor DECIMAL(24,6) NOT NULL,
      qfq_open DECIMAL(18,4) NOT NULL,
      qfq_high DECIMAL(18,4) NOT NULL,
      qfq_low DECIMAL(18,4) NOT NULL,
      qfq_close DECIMAL(18,4) NOT NULL,
      hfq_open DECIMAL(18,4) NOT NULL,
      hfq_high DECIMAL(18,4) NOT NULL,
      hfq_low DECIMAL(18,4) NOT NULL,
      hfq_close DECIMAL(18,4) NOT NULL,
      primary_source VARCHAR(96) NOT NULL,
      factor_source VARCHAR(96) NOT NULL,
      source_schema_version VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, business_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_daily_tradeability_facts (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      index_code VARCHAR(32) NOT NULL,
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
      source_schema_version VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, business_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_daily_verification_bars (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      business_date DATE NOT NULL,
      source VARCHAR(96) NOT NULL,
      exchange VARCHAR(16) NOT NULL,
      open_price DECIMAL(18,4) NOT NULL,
      high DECIMAL(18,4) NOT NULL,
      low DECIMAL(18,4) NOT NULL,
      close_price DECIMAL(18,4) NOT NULL,
      previous_close DECIMAL(18,4) NULL,
      volume_shares BIGINT NOT NULL,
      amount_cny DECIMAL(24,2) NOT NULL,
      turnover_percent DECIMAL(18,6) NULL,
      trade_status VARCHAR(64) NOT NULL,
      is_st TINYINT NULL,
      adjustment VARCHAR(32) NOT NULL,
      source_schema_version VARCHAR(64) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, business_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_daily_adjustment_events (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      effective_date DATE NOT NULL,
      qfq_factor DECIMAL(24,6) NOT NULL,
      hfq_factor DECIMAL(24,6) NOT NULL,
      source VARCHAR(96) NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, effective_date)
    )
    """,
)


def ensure_daily_schema(connection: Any) -> None:
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


PRIMARY_UPSERT = """
INSERT INTO m2_daily_primary_bars (
  dataset_id, symbol, business_date, source, exchange, open_price, high, low,
  close_price, previous_close, volume_shares, amount_cny, turnover_percent,
  trade_status, is_st, adjustment, source_schema_version, row_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  source=VALUES(source), exchange=VALUES(exchange), open_price=VALUES(open_price),
  high=VALUES(high), low=VALUES(low), close_price=VALUES(close_price),
  previous_close=VALUES(previous_close), volume_shares=VALUES(volume_shares),
  amount_cny=VALUES(amount_cny), turnover_percent=VALUES(turnover_percent),
  trade_status=VALUES(trade_status), is_st=VALUES(is_st), adjustment=VALUES(adjustment),
  source_schema_version=VALUES(source_schema_version), row_sha256=VALUES(row_sha256)
"""


ADJUSTED_UPSERT = """
INSERT INTO m2_daily_adjusted_bars (
  dataset_id, symbol, business_date, exchange, index_code, open_price, high, low,
  close_price, previous_close, volume_shares, amount_cny, turnover_percent,
  qfq_factor, hfq_factor, qfq_open, qfq_high, qfq_low, qfq_close,
  hfq_open, hfq_high, hfq_low, hfq_close, primary_source, factor_source,
  source_schema_version, row_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  exchange=VALUES(exchange), index_code=VALUES(index_code), open_price=VALUES(open_price),
  high=VALUES(high), low=VALUES(low), close_price=VALUES(close_price),
  previous_close=VALUES(previous_close), volume_shares=VALUES(volume_shares),
  amount_cny=VALUES(amount_cny), turnover_percent=VALUES(turnover_percent),
  qfq_factor=VALUES(qfq_factor), hfq_factor=VALUES(hfq_factor),
  qfq_open=VALUES(qfq_open), qfq_high=VALUES(qfq_high), qfq_low=VALUES(qfq_low), qfq_close=VALUES(qfq_close),
  hfq_open=VALUES(hfq_open), hfq_high=VALUES(hfq_high), hfq_low=VALUES(hfq_low), hfq_close=VALUES(hfq_close),
  primary_source=VALUES(primary_source), factor_source=VALUES(factor_source),
  source_schema_version=VALUES(source_schema_version), row_sha256=VALUES(row_sha256)
"""


TRADEABILITY_UPSERT = """
INSERT INTO m2_daily_tradeability_facts (
  dataset_id, symbol, business_date, index_code, has_primary_bar, has_secondary_status,
  is_suspended, is_st, listing_age_sessions, limit_rate, limit_up, limit_down,
  at_limit_up, at_limit_down, one_price_limit_up, one_price_limit_down,
  can_buy, can_sell, block_reasons_json, source_schema_version, row_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  index_code=VALUES(index_code), has_primary_bar=VALUES(has_primary_bar),
  has_secondary_status=VALUES(has_secondary_status), is_suspended=VALUES(is_suspended),
  is_st=VALUES(is_st), listing_age_sessions=VALUES(listing_age_sessions),
  limit_rate=VALUES(limit_rate), limit_up=VALUES(limit_up), limit_down=VALUES(limit_down),
  at_limit_up=VALUES(at_limit_up), at_limit_down=VALUES(at_limit_down),
  one_price_limit_up=VALUES(one_price_limit_up), one_price_limit_down=VALUES(one_price_limit_down),
  can_buy=VALUES(can_buy), can_sell=VALUES(can_sell), block_reasons_json=VALUES(block_reasons_json),
  source_schema_version=VALUES(source_schema_version), row_sha256=VALUES(row_sha256)
"""


VERIFICATION_UPSERT = PRIMARY_UPSERT.replace("m2_daily_primary_bars", "m2_daily_verification_bars")


ADJUSTMENT_UPSERT = """
INSERT INTO m2_daily_adjustment_events (
  dataset_id, symbol, effective_date, qfq_factor, hfq_factor, source, row_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  qfq_factor=VALUES(qfq_factor), hfq_factor=VALUES(hfq_factor),
  source=VALUES(source), row_sha256=VALUES(row_sha256)
"""


CHECKPOINT_UPSERT = """
INSERT INTO m2_daily_symbol_checkpoints (
  dataset_id, symbol, target_session, status, primary_present, adjusted_present,
  tradeability_present, verification_required, verification_present,
  reported_previous_close, primary_sha256, adjusted_sha256, tradeability_sha256,
  verification_sha256, adjustments_sha256, error_class, error_message
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  target_session=VALUES(target_session), status=VALUES(status),
  primary_present=VALUES(primary_present), adjusted_present=VALUES(adjusted_present),
  tradeability_present=VALUES(tradeability_present),
  verification_required=VALUES(verification_required), verification_present=VALUES(verification_present),
  reported_previous_close=VALUES(reported_previous_close), primary_sha256=VALUES(primary_sha256),
  adjusted_sha256=VALUES(adjusted_sha256), tradeability_sha256=VALUES(tradeability_sha256),
  verification_sha256=VALUES(verification_sha256), adjustments_sha256=VALUES(adjustments_sha256),
  error_class=VALUES(error_class), error_message=VALUES(error_message)
"""


RUN_INSERT = """
INSERT INTO m2_daily_runs (
  dataset_id, schema_version, manifest_version, target_session, previous_session,
  snapshot_effective_session, base_history_dataset_id, predecessor_dataset_id,
  scope_sha256, authoritative, simulation_orders_allowed, accepted,
  expected_symbol_count, primary_row_count, adjusted_row_count,
  tradeability_row_count, verification_row_count, adjustment_event_count,
  primary_sha256, adjusted_sha256, tradeability_sha256, verification_sha256,
  adjustments_sha256, quality_sha256, manifest_sha256, manifest_json
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _primary_rows(dataset_id: str, rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [(
        dataset_id, row["symbol"], row["business_date"], row["source"], row["exchange"],
        row["open"], row["high"], row["low"], row["close"], row.get("previous_close"),
        int(row["volume_shares"]), row["amount_cny"], row.get("turnover_percent"),
        row["trade_status"], _bool(row.get("is_st")), row["adjustment"],
        row["schema_version"], sha256(row),
    ) for row in rows]


def _adjusted_rows(dataset_id: str, rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [(
        dataset_id, row["symbol"], row["business_date"], row["exchange"], row["index_code"],
        row["open"], row["high"], row["low"], row["close"], row["previous_close"],
        int(row["volume_shares"]), row["amount_cny"], row.get("turnover_percent"),
        row["qfq_factor"], row["hfq_factor"], row["qfq_open"], row["qfq_high"],
        row["qfq_low"], row["qfq_close"], row["hfq_open"], row["hfq_high"],
        row["hfq_low"], row["hfq_close"], row["primary_source"], row["factor_source"],
        row["schema_version"], sha256(row),
    ) for row in rows]


def _fact_rows(dataset_id: str, rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [(
        dataset_id, row["symbol"], row["business_date"], row["index_code"],
        _bool(row["has_primary_bar"]), _bool(row["has_secondary_status"]),
        _bool(row["is_suspended"]), _bool(row.get("is_st")), int(row["listing_age_sessions"]),
        row.get("limit_rate"), row.get("limit_up"), row.get("limit_down"),
        _bool(row["at_limit_up"]), _bool(row["at_limit_down"]),
        _bool(row["one_price_limit_up"]), _bool(row["one_price_limit_down"]),
        _bool(row["can_buy"]), _bool(row["can_sell"]), _compact(row["block_reasons"]),
        row["schema_version"], sha256(row),
    ) for row in rows]


def _event_rows(dataset_id: str, rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [(
        dataset_id, row["symbol"], row["effective_date"], row["qfq_factor"],
        row["hfq_factor"], row["source"], sha256(row),
    ) for row in rows]


def _symbols(evidence: DailyEvidence) -> set[str]:
    return {
        str(row["symbol"])
        for collection in (
            evidence.primary_bars, evidence.tradeability, evidence.verification_bars,
            evidence.adjusted_bars, evidence.adjustments,
        )
        for row in collection
    }


def publish_daily_symbol_checkpoint(
    connection: Any,
    evidence: DailyEvidence,
    *,
    dataset_id: str,
    symbol: str,
    target_session: date,
    verification_required: bool,
    reported_previous_close: Decimal | None,
    status: str,
    error: Exception | str | None = None,
) -> dict[str, int]:
    """Atomically replace one mutable checkpoint while its run is unaccepted."""
    if status not in {"succeeded", "blocked", "failed"}:
        raise ValueError(f"unsupported daily checkpoint status: {status}")
    if evidence.manifest.get("authoritative") is not False or evidence.manifest.get("simulation_orders_allowed") is not False:
        raise RuntimeError("daily checkpoints must be explicitly non-authoritative and simulation-only")
    observed_symbols = _symbols(evidence)
    if observed_symbols - {symbol}:
        raise ValueError(f"daily symbol checkpoint contains other symbols: {sorted(observed_symbols)}")
    primary_rows = [row for row in evidence.primary_bars if row.get("symbol") == symbol]
    adjusted_rows = [row for row in evidence.adjusted_bars if row.get("symbol") == symbol]
    fact_rows = [row for row in evidence.tradeability if row.get("symbol") == symbol]
    verification_rows = [row for row in evidence.verification_bars if row.get("symbol") == symbol]
    event_rows = [row for row in evidence.adjustments if row.get("symbol") == symbol]
    if len(primary_rows) > 1 or len(adjusted_rows) > 1 or len(fact_rows) > 1 or len(verification_rows) > 1:
        raise ValueError("daily symbol checkpoint permits at most one row per evidence type")
    if status == "succeeded" and len(fact_rows) != 1:
        raise ValueError("succeeded daily checkpoint requires a tradeability fact")
    if primary_rows and len(adjusted_rows) != 1:
        raise ValueError("a stored daily primary bar requires one adjusted bar")
    if verification_required and status == "succeeded" and primary_rows and len(verification_rows) != 1:
        raise ValueError("succeeded verification target requires an independent verification row")

    with connection.cursor() as cursor:
        cursor.execute("SELECT accepted FROM m2_daily_runs WHERE dataset_id=%s", (dataset_id,))
        accepted_rows = cursor.fetchall()
        if accepted_rows and bool(accepted_rows[0][0]):
            raise RuntimeError(f"accepted daily dataset is immutable: {dataset_id}")
        for table in (
            "m2_daily_primary_bars", "m2_daily_adjusted_bars",
            "m2_daily_tradeability_facts", "m2_daily_verification_bars",
            "m2_daily_adjustment_events",
        ):
            cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s AND symbol=%s", (dataset_id, symbol))

    error_text = None if error is None else str(error)
    error_class = None if error is None else type(error).__name__ if isinstance(error, Exception) else "RuntimeError"
    counts = {
        "primary_bars": _upsert_many(connection, PRIMARY_UPSERT, _primary_rows(dataset_id, primary_rows)),
        "adjusted_bars": _upsert_many(connection, ADJUSTED_UPSERT, _adjusted_rows(dataset_id, adjusted_rows)),
        "tradeability": _upsert_many(connection, TRADEABILITY_UPSERT, _fact_rows(dataset_id, fact_rows)),
        "verification_bars": _upsert_many(connection, VERIFICATION_UPSERT, _primary_rows(dataset_id, verification_rows)),
        "adjustments": _upsert_many(connection, ADJUSTMENT_UPSERT, _event_rows(dataset_id, event_rows)),
    }
    checkpoint = (
        dataset_id, symbol, target_session.isoformat(), status, int(bool(primary_rows)),
        int(bool(adjusted_rows)), int(bool(fact_rows)), int(verification_required),
        int(bool(verification_rows)), _decimal(reported_previous_close),
        sha256(primary_rows) if primary_rows else None,
        sha256(adjusted_rows) if adjusted_rows else None,
        sha256(fact_rows) if fact_rows else None,
        sha256(verification_rows) if verification_rows else None,
        sha256(event_rows) if event_rows else None,
        error_class, error_text,
    )
    counts["symbol_checkpoints"] = _upsert_many(connection, CHECKPOINT_UPSERT, [checkpoint])
    connection.commit()
    return counts


def _query_all(connection: Any, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def load_daily_checkpoint_evidence(connection: Any, dataset_id: str) -> tuple[DailyEvidence, dict[str, Any]]:
    checkpoints = _query_all(connection, """
        SELECT symbol, status, verification_required, reported_previous_close, error_class, error_message,
               primary_sha256, adjusted_sha256, tradeability_sha256,
               verification_sha256, adjustments_sha256
        FROM m2_daily_symbol_checkpoints
        WHERE dataset_id=%s AND status IN ('succeeded', 'blocked') ORDER BY symbol
    """, (dataset_id,))
    retained = {str(row[0]) for row in checkpoints}
    metadata = {
        "succeeded_symbols": sorted(str(row[0]) for row in checkpoints if str(row[1]) == "succeeded"),
        "blocked_symbols": sorted(str(row[0]) for row in checkpoints if str(row[1]) == "blocked"),
        "verification_required_symbols": sorted(str(row[0]) for row in checkpoints if bool(row[2])),
        "reported_previous_closes": {
            str(row[0]): Decimal(str(row[3])) for row in checkpoints if row[3] is not None
        },
        "errors": {
            str(row[0]): f"{row[4] or 'RuntimeError'}: {row[5] or 'incomplete daily evidence'}"
            for row in checkpoints if row[4] or row[5]
        },
    }
    if not retained:
        return DailyEvidence(
            manifest={"authoritative": False, "simulation_orders_allowed": False},
            primary_bars=[], tradeability=[], verification_bars=[], adjusted_bars=[], adjustments=[],
        ), metadata

    def keep(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [row for row in rows if str(row[0]) in retained]

    primary = keep(_query_all(connection, """
        SELECT symbol, business_date, source, exchange, open_price, high, low, close_price,
               previous_close, volume_shares, amount_cny, turnover_percent, trade_status,
               is_st, adjustment, source_schema_version, row_sha256
        FROM m2_daily_primary_bars WHERE dataset_id=%s ORDER BY symbol, business_date
    """, (dataset_id,)))
    adjusted = keep(_query_all(connection, """
        SELECT symbol, business_date, exchange, index_code, open_price, high, low, close_price,
               previous_close, volume_shares, amount_cny, turnover_percent, qfq_factor, hfq_factor,
               qfq_open, qfq_high, qfq_low, qfq_close, hfq_open, hfq_high, hfq_low, hfq_close,
               primary_source, factor_source, source_schema_version, row_sha256
        FROM m2_daily_adjusted_bars WHERE dataset_id=%s ORDER BY symbol, business_date
    """, (dataset_id,)))
    facts = keep(_query_all(connection, """
        SELECT symbol, business_date, index_code, has_primary_bar, has_secondary_status,
               is_suspended, is_st, listing_age_sessions, limit_rate, limit_up, limit_down,
               at_limit_up, at_limit_down, one_price_limit_up, one_price_limit_down,
               can_buy, can_sell, block_reasons_json, source_schema_version
               , row_sha256
        FROM m2_daily_tradeability_facts WHERE dataset_id=%s ORDER BY symbol, business_date
    """, (dataset_id,)))
    verification = keep(_query_all(connection, """
        SELECT symbol, business_date, source, exchange, open_price, high, low, close_price,
               previous_close, volume_shares, amount_cny, turnover_percent, trade_status,
               is_st, adjustment, source_schema_version, row_sha256
        FROM m2_daily_verification_bars WHERE dataset_id=%s ORDER BY symbol, business_date
    """, (dataset_id,)))
    events = keep(_query_all(connection, """
        SELECT symbol, effective_date, qfq_factor, hfq_factor, source, row_sha256
        FROM m2_daily_adjustment_events WHERE dataset_id=%s ORDER BY symbol, effective_date
    """, (dataset_id,)))

    def daily_bar(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "symbol": str(row[0]), "business_date": _date_text(row[1]), "source": row[2],
            "exchange": row[3], "open": _decimal(row[4]), "high": _decimal(row[5]),
            "low": _decimal(row[6]), "close": _decimal(row[7]), "previous_close": _decimal(row[8]),
            "volume_shares": int(row[9]), "amount_cny": _decimal(row[10]),
            "turnover_percent": _decimal(row[11]), "trade_status": row[12],
            "is_st": None if row[13] is None else bool(row[13]), "adjustment": row[14],
            "schema_version": row[15],
        }

    canonical_primary = [daily_bar(row) for row in primary]
    canonical_adjusted = [{
        "symbol": str(row[0]), "business_date": _date_text(row[1]), "exchange": row[2],
        "index_code": row[3], "open": _decimal(row[4]), "high": _decimal(row[5]),
        "low": _decimal(row[6]), "close": _decimal(row[7]), "previous_close": _decimal(row[8]),
        "volume_shares": int(row[9]), "amount_cny": _decimal(row[10]),
        "turnover_percent": _decimal(row[11]), "qfq_factor": _decimal(row[12]),
        "hfq_factor": _decimal(row[13]), "qfq_open": _decimal(row[14]),
        "qfq_high": _decimal(row[15]), "qfq_low": _decimal(row[16]),
        "qfq_close": _decimal(row[17]), "hfq_open": _decimal(row[18]),
        "hfq_high": _decimal(row[19]), "hfq_low": _decimal(row[20]),
        "hfq_close": _decimal(row[21]), "primary_source": row[22],
        "factor_source": row[23], "schema_version": row[24],
    } for row in adjusted]
    canonical_facts = [{
        "symbol": str(row[0]), "business_date": _date_text(row[1]), "index_code": row[2],
        "has_primary_bar": bool(row[3]), "has_secondary_status": bool(row[4]),
        "is_suspended": bool(row[5]), "is_st": None if row[6] is None else bool(row[6]),
        "listing_age_sessions": int(row[7]), "limit_rate": _decimal(row[8]),
        "limit_up": _decimal(row[9]), "limit_down": _decimal(row[10]),
        "at_limit_up": bool(row[11]), "at_limit_down": bool(row[12]),
        "one_price_limit_up": bool(row[13]), "one_price_limit_down": bool(row[14]),
        "can_buy": bool(row[15]), "can_sell": bool(row[16]),
        "block_reasons": json.loads(row[17]), "schema_version": row[18],
    } for row in facts]
    canonical_verification = [daily_bar(row) for row in verification]
    canonical_events = [{
        "symbol": str(row[0]), "effective_date": _date_text(row[1]),
        "qfq_factor": _decimal(row[2]), "hfq_factor": _decimal(row[3]), "source": row[4],
    } for row in events]

    for label, raw_rows, canonical_rows in (
        ("primary", primary, canonical_primary),
        ("adjusted", adjusted, canonical_adjusted),
        ("tradeability", facts, canonical_facts),
        ("verification", verification, canonical_verification),
        ("adjustments", events, canonical_events),
    ):
        for raw_row, canonical_row in zip(raw_rows, canonical_rows, strict=True):
            if str(raw_row[-1]) != sha256(canonical_row):
                raise RuntimeError(
                    f"daily {label} row hash mismatch for {canonical_row.get('symbol')}"
                )

    evidence = DailyEvidence(
        manifest={"authoritative": False, "simulation_orders_allowed": False},
        primary_bars=canonical_primary,
        adjusted_bars=canonical_adjusted,
        tradeability=canonical_facts,
        verification_bars=canonical_verification,
        adjustments=canonical_events,
    )
    collections = {
        "primary": evidence.primary_bars,
        "adjusted": evidence.adjusted_bars,
        "tradeability": evidence.tradeability,
        "verification": evidence.verification_bars,
        "adjustments": evidence.adjustments,
    }
    checkpoint_hash_positions = {
        "primary": 6, "adjusted": 7, "tradeability": 8,
        "verification": 9, "adjustments": 10,
    }
    for checkpoint in checkpoints:
        symbol = str(checkpoint[0])
        for label, position in checkpoint_hash_positions.items():
            symbol_rows = [row for row in collections[label] if str(row["symbol"]) == symbol]
            expected_hash = sha256(symbol_rows) if symbol_rows else None
            stored_hash = None if checkpoint[position] is None else str(checkpoint[position])
            if stored_hash != expected_hash:
                raise RuntimeError(
                    f"daily checkpoint {label} hash mismatch for {symbol}: "
                    f"stored={stored_hash} actual={expected_hash}"
                )
    return evidence, metadata


def load_previous_adjusted_states(
    connection: Any,
    *,
    predecessor_dataset_id: str,
    previous_session: date,
) -> dict[str, PreviousAdjustedState]:
    daily_run = _query_all(connection, """
        SELECT target_session, accepted, authoritative, simulation_orders_allowed
        FROM m2_daily_runs WHERE dataset_id=%s
    """, (predecessor_dataset_id,))
    if daily_run:
        if (
            _date_text(daily_run[0][0]) != previous_session.isoformat()
            or not bool(daily_run[0][1]) or bool(daily_run[0][2]) or bool(daily_run[0][3])
        ):
            raise RuntimeError("daily predecessor is not the exact accepted previous session")
        rows = _query_all(connection, """
            SELECT symbol, business_date, close_price, qfq_factor, hfq_factor
            FROM m2_daily_adjusted_bars
            WHERE dataset_id=%s AND business_date=%s ORDER BY symbol
        """, (predecessor_dataset_id, previous_session.isoformat()))
    else:
        history_run = _query_all(connection, """
            SELECT business_end, accepted, authoritative, simulation_orders_allowed
            FROM m2_history_runs WHERE dataset_id=%s
        """, (predecessor_dataset_id,))
        if not history_run:
            raise RuntimeError(f"predecessor dataset does not exist: {predecessor_dataset_id}")
        row = history_run[0]
        if _date_text(row[0]) != previous_session.isoformat() or not bool(row[1]):
            raise RuntimeError("history predecessor is not the exact accepted previous session")
        if bool(row[2]) or bool(row[3]):
            raise RuntimeError("history predecessor violates the research-only boundary")
        rows = _query_all(connection, """
            SELECT b.symbol, b.business_date, b.close_price, b.qfq_factor, b.hfq_factor
            FROM m2_history_run_shards s
            JOIN m2_historical_bars b ON b.dataset_id=s.shard_dataset_id
            WHERE s.merged_dataset_id=%s AND b.business_date=%s ORDER BY b.symbol
        """, (predecessor_dataset_id, previous_session.isoformat()))
    states: dict[str, PreviousAdjustedState] = {}
    for row in rows:
        symbol = str(row[0])
        if symbol in states:
            raise RuntimeError(f"duplicate predecessor adjusted state for {symbol}")
        states[symbol] = PreviousAdjustedState(
            symbol=symbol,
            business_date=date.fromisoformat(_date_text(row[1])),
            raw_close=Decimal(str(row[2])),
            qfq_factor=Decimal(str(row[3])),
            hfq_factor=Decimal(str(row[4])),
            source_dataset_id=predecessor_dataset_id,
        )
    return states


def load_base_references(connection: Any, base_history_dataset_id: str) -> dict[str, date]:
    rows = _query_all(connection, """
        SELECT r.symbol, r.ipo_date
        FROM m2_history_run_shards s
        JOIN m2_security_references r ON r.dataset_id=s.shard_dataset_id
        WHERE s.merged_dataset_id=%s ORDER BY r.symbol
    """, (base_history_dataset_id,))
    result: dict[str, date] = {}
    for symbol, ipo_date in rows:
        code = str(symbol)
        parsed = date.fromisoformat(_date_text(ipo_date))
        if code in result and result[code] != parsed:
            raise RuntimeError(f"conflicting base IPO dates for {code}")
        result[code] = parsed
    return result


def latest_accepted_lineage(
    connection: Any,
    base_history_dataset_id: str,
) -> tuple[date, str]:
    base = _query_all(connection, """
        SELECT business_end, accepted, authoritative, simulation_orders_allowed
        FROM m2_history_runs WHERE dataset_id=%s
    """, (base_history_dataset_id,))
    if len(base) != 1 or not bool(base[0][1]) or bool(base[0][2]) or bool(base[0][3]):
        raise RuntimeError("base history dataset is missing, unaccepted, or outside research-only boundaries")
    base_session = date.fromisoformat(_date_text(base[0][0]))
    rows = _query_all(connection, """
        SELECT dataset_id, target_session, previous_session, predecessor_dataset_id,
               authoritative, simulation_orders_allowed
        FROM m2_daily_runs
        WHERE base_history_dataset_id=%s AND accepted=1 ORDER BY target_session
    """, (base_history_dataset_id,))
    previous_session = base_session
    predecessor_id = base_history_dataset_id
    for row in rows:
        dataset_id = str(row[0])
        target_session = date.fromisoformat(_date_text(row[1]))
        recorded_previous = date.fromisoformat(_date_text(row[2]))
        recorded_predecessor = str(row[3])
        if bool(row[4]) or bool(row[5]):
            raise RuntimeError(f"daily lineage violates research-only boundaries: {dataset_id}")
        if recorded_previous != previous_session or recorded_predecessor != predecessor_id:
            raise RuntimeError(
                f"daily lineage gap or predecessor mismatch at {dataset_id}: "
                f"expected {previous_session}/{predecessor_id}, "
                f"got {recorded_previous}/{recorded_predecessor}"
            )
        if target_session <= recorded_previous:
            raise RuntimeError(f"daily lineage target does not advance: {dataset_id}")
        previous_session = target_session
        predecessor_id = dataset_id
    return previous_session, predecessor_id


def _manifest_hashes(evidence: DailyEvidence) -> dict[str, Any]:
    return {
        "primary_row_count": len(evidence.primary_bars),
        "adjusted_row_count": len(evidence.adjusted_bars),
        "tradeability_row_count": len(evidence.tradeability),
        "verification_row_count": len(evidence.verification_bars),
        "adjustment_event_count": len(evidence.adjustments),
        "primary_sha256": sha256(evidence.primary_bars),
        "adjusted_sha256": sha256(evidence.adjusted_bars),
        "tradeability_sha256": sha256(evidence.tradeability),
        "verification_sha256": sha256(evidence.verification_bars),
        "adjustments_sha256": sha256(evidence.adjustments),
    }


def publish_daily_run(
    connection: Any,
    evidence: DailyEvidence,
    *,
    dataset_id: str,
    base_history_dataset_id: str,
    predecessor_dataset_id: str,
) -> dict[str, Any]:
    """Atomically expose one already-checkpointed accepted daily package."""
    manifest = evidence.manifest
    if manifest.get("accepted") is not True:
        raise RuntimeError("refusing to publish an unaccepted daily aggregate")
    if manifest.get("authoritative") is not False or manifest.get("simulation_orders_allowed") is not False:
        raise RuntimeError("daily aggregate must remain non-authoritative and simulation-only")
    expected = _manifest_hashes(evidence)
    mismatches = {
        key: {"manifest": manifest.get(key), "stored": value}
        for key, value in expected.items() if manifest.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"daily aggregate physical evidence mismatch: {mismatches}")
    target = str(manifest["target_session"])
    scope_hash = str(manifest["scope_sha256"])
    if dataset_id != default_daily_dataset_id(target, scope_hash):
        raise RuntimeError("daily dataset id does not match target session and scope hash")
    expected_symbols = int(manifest["expected_symbol_count"])
    facts = {str(row["symbol"]) for row in evidence.tradeability}
    if len(facts) != expected_symbols:
        raise RuntimeError(f"daily aggregate requires {expected_symbols} complete tradeability facts, got {len(facts)}")
    checkpoints = _query_all(connection, """
        SELECT symbol, status FROM m2_daily_symbol_checkpoints
        WHERE dataset_id=%s ORDER BY symbol
    """, (dataset_id,))
    completed = {str(row[0]) for row in checkpoints if str(row[1]) in {"succeeded", "blocked"}}
    if completed != facts:
        raise RuntimeError("daily aggregate checkpoint inventory does not match tradeability evidence")

    manifest_hash = sha256(manifest)
    existing = _query_all(connection, """
        SELECT dataset_id, manifest_sha256 FROM m2_daily_runs WHERE target_session=%s AND accepted=1
    """, (target,))
    if existing:
        if len(existing) == 1 and str(existing[0][0]) == dataset_id and str(existing[0][1]) == manifest_hash:
            return {"dataset_id": dataset_id, "accepted": True, "idempotent_replay": True}
        raise RuntimeError(f"a different accepted daily result already exists for {target}")

    row = (
        dataset_id, DAILY_STORE_SCHEMA_VERSION, manifest["manifest_version"], target,
        manifest["previous_session"], manifest["snapshot_effective_session"],
        base_history_dataset_id, predecessor_dataset_id, scope_hash, 0, 0, 1,
        expected_symbols, expected["primary_row_count"], expected["adjusted_row_count"],
        expected["tradeability_row_count"], expected["verification_row_count"],
        expected["adjustment_event_count"], expected["primary_sha256"],
        expected["adjusted_sha256"], expected["tradeability_sha256"],
        expected["verification_sha256"], expected["adjustments_sha256"],
        manifest["quality_sha256"], manifest_hash, _compact(manifest),
    )
    with connection.cursor() as cursor:
        cursor.execute(RUN_INSERT, row)
    connection.commit()
    return {"dataset_id": dataset_id, "accepted": True, "idempotent_replay": False}


__all__ = [
    "DAILY_STORE_SCHEMA_VERSION", "DailyEvidence", "TiDBConfig", "connect",
    "default_daily_dataset_id", "ensure_daily_schema", "latest_accepted_lineage",
    "load_base_references", "load_daily_checkpoint_evidence", "load_daily_evidence",
    "load_previous_adjusted_states", "publish_daily_run", "publish_daily_symbol_checkpoint",
]

"""TiDB checkpoints and atomic publication for M2 daily market increments.

Partial symbol rows are resumable research checkpoints.  They become visible to
future consumers only after one accepted aggregate run is published.  All data
remains non-authoritative and cannot create simulated orders by itself.
"""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from scripts.market_data.contracts import DailyBar, normalize_symbol, parse_date
from scripts.market_data.daily_adjustments import PreviousAdjustedState, build_daily_adjusted_bars
from scripts.market_data.daily_quality_gates import cross_source_consistency_errors
from scripts.market_data.historical_contracts import AdjustmentEvent
from scripts.market_data.manifest import sha256
from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect as _connect_once


DAILY_STORE_SCHEMA_VERSION = "m2-tidb-daily-checkpoint-v6"
DAILY_LINEAGE_SCHEMA_VERSION = "m2-daily-lineage-evidence-v1"
TRADEABILITY_QUANTUM = Decimal("0.01")
STORAGE_PRICE_QUANTUM = Decimal("0.0001")
STORAGE_AMOUNT_QUANTUM = Decimal("0.01")
STORAGE_RATIO_QUANTUM = Decimal("0.000001")
TIDB_CONNECT_ATTEMPTS = 3
TIDB_TRANSIENT_ERROR_CODES = frozenset({2003, 2006, 2013})


def _is_transient_connect_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    error_code = error.args[0] if error.args else None
    return isinstance(error_code, int) and error_code in TIDB_TRANSIENT_ERROR_CODES


def connect(config: TiDBConfig):
    """Open a TiDB connection with bounded retries for transient network faults."""
    for attempt in range(1, TIDB_CONNECT_ATTEMPTS + 1):
        try:
            return _connect_once(config)
        except Exception as error:
            if not _is_transient_connect_error(error) or attempt == TIDB_CONNECT_ATTEMPTS:
                raise
            time.sleep(2 ** (attempt - 1))
    raise AssertionError("unreachable TiDB connection retry state")


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal(value: Any) -> str | None:
    return None if value is None else format(value, "f")


def _bool(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _date_text(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _tradeability_decimal(value: Any, field: str) -> str | None:
    """Return the point-in-time price-limit contract's stable two-decimal form."""
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
        normalized = parsed.quantize(TRADEABILITY_QUANTUM)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid {field}: {value!r}") from error
    if not parsed.is_finite() or parsed != normalized:
        raise ValueError(f"{field} exceeds the two-decimal tradeability contract: {value!r}")
    return format(normalized, "f")


def _storage_decimal(
    value: Any,
    quantum: Decimal,
    field: str,
    *,
    allow_none: bool = False,
) -> str | None:
    """Canonicalize a number exactly as TiDB DECIMAL stores it."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field} cannot be null")
    try:
        parsed = Decimal(str(value))
        normalized = parsed.quantize(quantum)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid {field}: {value!r}") from error
    if not parsed.is_finite() or parsed != normalized:
        raise ValueError(f"{field} exceeds TiDB storage precision: {value!r}")
    return format(normalized, "f")


def _canonical_adjusted_bar(row: Mapping[str, Any]) -> dict[str, Any]:
    price_fields = (
        "open", "high", "low", "close", "previous_close",
        "qfq_open", "qfq_high", "qfq_low", "qfq_close",
        "hfq_open", "hfq_high", "hfq_low", "hfq_close",
    )
    canonical = {
        "symbol": str(row["symbol"]),
        "business_date": _date_text(row["business_date"]),
        "exchange": str(row["exchange"]),
        "index_code": str(row["index_code"]),
        **{
            field: _storage_decimal(row.get(field), STORAGE_PRICE_QUANTUM, field)
            for field in price_fields
        },
        "volume_shares": int(row["volume_shares"]),
        "amount_cny": _storage_decimal(
            row.get("amount_cny"), STORAGE_AMOUNT_QUANTUM, "amount_cny",
        ),
        "turnover_percent": _storage_decimal(
            row.get("turnover_percent"), STORAGE_RATIO_QUANTUM, "turnover_percent",
            allow_none=True,
        ),
        "qfq_factor": _storage_decimal(
            row.get("qfq_factor"), STORAGE_RATIO_QUANTUM, "qfq_factor",
        ),
        "hfq_factor": _storage_decimal(
            row.get("hfq_factor"), STORAGE_RATIO_QUANTUM, "hfq_factor",
        ),
        "primary_source": str(row["primary_source"]),
        "factor_source": str(row["factor_source"]),
        "schema_version": str(row["schema_version"]),
    }
    return canonical


def _canonical_tradeability_fact(row: Mapping[str, Any]) -> dict[str, Any]:
    reasons = row["block_reasons"]
    if not isinstance(reasons, (list, tuple)) or not all(isinstance(reason, str) for reason in reasons):
        raise ValueError("tradeability block_reasons must be a list or tuple of strings")
    return {
        "symbol": str(row["symbol"]),
        "business_date": _date_text(row["business_date"]),
        "index_code": str(row["index_code"]),
        "has_primary_bar": bool(row["has_primary_bar"]),
        "has_secondary_status": bool(row["has_secondary_status"]),
        "is_suspended": bool(row["is_suspended"]),
        "is_st": None if row.get("is_st") is None else bool(row["is_st"]),
        "listing_age_sessions": int(row["listing_age_sessions"]),
        "limit_rate": _tradeability_decimal(row.get("limit_rate"), "limit_rate"),
        "limit_up": _tradeability_decimal(row.get("limit_up"), "limit_up"),
        "limit_down": _tradeability_decimal(row.get("limit_down"), "limit_down"),
        "at_limit_up": bool(row["at_limit_up"]),
        "at_limit_down": bool(row["at_limit_down"]),
        "one_price_limit_up": bool(row["one_price_limit_up"]),
        "one_price_limit_down": bool(row["one_price_limit_down"]),
        "can_buy": bool(row["can_buy"]),
        "can_sell": bool(row["can_sell"]),
        "block_reasons": list(reasons),
        "schema_version": str(row["schema_version"]),
    }


def _read_gzip_json(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"invalid daily evidence file: {path}")
    return value


def canonical_lineage_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the small immutable evidence envelope used by V4 recovery paths."""
    symbol = str(row.get("symbol", ""))
    if normalize_symbol(symbol) != symbol:
        raise ValueError("daily lineage evidence requires a normalized symbol")
    target_session = _date_text(row.get("target_session"))
    parse_date(target_session)
    kind = str(row.get("kind", "")).strip()
    if kind not in {"cash_dividend_reference", "gap_no_adjustment_recovery"}:
        raise ValueError(f"unsupported daily lineage evidence kind: {kind}")
    source = str(row.get("source", "")).strip()
    if not source:
        raise ValueError("daily lineage evidence requires an attributed source")
    details = row.get("details")
    if not isinstance(details, Mapping) or not details:
        raise ValueError("daily lineage evidence requires nonempty structured details")
    canonical_details = json.loads(_compact(dict(details)))
    target = parse_date(target_session)
    if kind == "cash_dividend_reference":
        if source != "tencent_archive":
            raise ValueError("cash-dividend lineage evidence requires Tencent attribution")
        previous = parse_date(canonical_details.get("previous_session"))
        registration = parse_date(canonical_details.get("registration_date"))
        ex_rights = parse_date(canonical_details.get("ex_rights_date"))
        accepted_close = Decimal(str(canonical_details.get("accepted_previous_close")))
        cash_per_ten = Decimal(str(canonical_details.get("cash_per_ten_shares")))
        derived_close = Decimal(str(canonical_details.get("derived_previous_close")))
        if previous != registration or ex_rights != target or not previous < target:
            raise ValueError("cash-dividend lineage dates do not match the daily transition")
        expected_close = (accepted_close - cash_per_ten / Decimal("10")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if min(accepted_close, cash_per_ten, derived_close) <= 0 or derived_close != expected_close:
            raise ValueError("cash-dividend lineage arithmetic does not reconcile")
        if not str(canonical_details.get("action_content", "")).strip():
            raise ValueError("cash-dividend lineage requires the vendor action content")
        vendor_hash = str(canonical_details.get("vendor_action_sha256", ""))
        if len(vendor_hash) != 64 or any(character not in "0123456789abcdef" for character in vendor_hash):
            raise ValueError("cash-dividend lineage requires a valid vendor action hash")
    else:
        if source != "tencent_raw_hfq_continuity":
            raise ValueError("gap recovery lineage evidence requires Tencent continuity attribution")
        prior = parse_date(canonical_details.get("prior_session"))
        recovered = parse_date(canonical_details.get("recovered_session"))
        observed_sessions = [parse_date(value) for value in canonical_details.get("observed_sessions", [])]
        maximum_change = Decimal(str(canonical_details.get("maximum_implied_hfq_change_rate")))
        numeric_values = [
            Decimal(str(canonical_details.get(key)))
            for key in (
                "accepted_prior_close", "recovered_raw_close", "qfq_factor", "hfq_factor",
            )
        ]
        if not prior < recovered < target:
            raise ValueError("gap recovery lineage dates do not precede the target session")
        if observed_sessions != sorted(set(observed_sessions)) or not observed_sessions:
            raise ValueError("gap recovery lineage sessions must be unique and sorted")
        if observed_sessions[0] != prior or observed_sessions[-1] != recovered:
            raise ValueError("gap recovery lineage sessions do not cover both boundaries")
        if any(value <= 0 for value in numeric_values) or not Decimal("0") <= maximum_change <= Decimal("0.002"):
            raise ValueError("gap recovery lineage values violate continuity boundaries")
        if not str(canonical_details.get("prior_source_dataset_id", "")).strip():
            raise ValueError("gap recovery lineage requires the immutable prior dataset")
        for key in ("raw_rows_sha256", "hfq_rows_sha256"):
            value = str(canonical_details.get(key, ""))
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"gap recovery lineage requires a valid {key}")
    return {
        "schema_version": DAILY_LINEAGE_SCHEMA_VERSION,
        "symbol": symbol,
        "target_session": target_session,
        "kind": kind,
        "source": source,
        "details": canonical_details,
    }


@dataclass(frozen=True, slots=True)
class DailyEvidence:
    manifest: dict[str, Any]
    primary_bars: list[dict[str, Any]]
    tradeability: list[dict[str, Any]]
    verification_bars: list[dict[str, Any]]
    adjusted_bars: list[dict[str, Any]]
    adjustments: list[dict[str, Any]]
    lineage_evidence: list[dict[str, Any]] = field(default_factory=list)


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
        lineage_evidence=(
            _read_gzip_json(input_dir / "daily-lineage-evidence.json.gz")
            if (input_dir / "daily-lineage-evidence.json.gz").exists()
            else []
        ),
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
      lineage_evidence_count INT NOT NULL,
      primary_sha256 CHAR(64) NOT NULL,
      adjusted_sha256 CHAR(64) NOT NULL,
      tradeability_sha256 CHAR(64) NOT NULL,
      verification_sha256 CHAR(64) NOT NULL,
      adjustments_sha256 CHAR(64) NOT NULL,
      lineage_evidence_sha256 CHAR(64) NOT NULL,
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
      status_source VARCHAR(96) NULL,
      reported_previous_close_source VARCHAR(96) NULL,
      checkpoint_origin_dataset_id VARCHAR(160) NULL,
      primary_sha256 CHAR(64) NULL,
      adjusted_sha256 CHAR(64) NULL,
      tradeability_sha256 CHAR(64) NULL,
      verification_sha256 CHAR(64) NULL,
      adjustments_sha256 CHAR(64) NULL,
      lineage_evidence_sha256 CHAR(64) NULL,
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
    """
    CREATE TABLE IF NOT EXISTS m2_daily_run_supersessions (
      superseded_dataset_id VARCHAR(160) NOT NULL PRIMARY KEY,
      replacement_dataset_id VARCHAR(160) NOT NULL,
      target_session DATE NOT NULL,
      reason_code VARCHAR(96) NOT NULL,
      evidence_sha256 CHAR(64) NOT NULL,
      evidence_json LONGTEXT NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      UNIQUE KEY uq_m2_daily_replacement (replacement_dataset_id),
      KEY idx_m2_daily_supersessions_target (target_session)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_daily_lineage_evidence (
      dataset_id VARCHAR(160) NOT NULL,
      symbol CHAR(6) NOT NULL,
      target_session DATE NOT NULL,
      evidence_kind VARCHAR(64) NOT NULL,
      source VARCHAR(96) NOT NULL,
      evidence_json LONGTEXT NOT NULL,
      row_sha256 CHAR(64) NOT NULL,
      published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (dataset_id, symbol, evidence_kind)
    )
    """,
    """
    ALTER TABLE m2_daily_symbol_checkpoints
      ADD COLUMN IF NOT EXISTS status_source VARCHAR(96) NULL AFTER reported_previous_close
    """,
    """
    ALTER TABLE m2_daily_symbol_checkpoints
      ADD COLUMN IF NOT EXISTS reported_previous_close_source VARCHAR(96) NULL AFTER status_source
    """,
    """
    ALTER TABLE m2_daily_symbol_checkpoints
      ADD COLUMN IF NOT EXISTS checkpoint_origin_dataset_id VARCHAR(160) NULL
      AFTER reported_previous_close_source
    """,
    """
    ALTER TABLE m2_daily_symbol_checkpoints
      ADD COLUMN IF NOT EXISTS lineage_evidence_sha256 CHAR(64) NULL
      AFTER adjustments_sha256
    """,
    """
    ALTER TABLE m2_daily_runs
      ADD COLUMN IF NOT EXISTS lineage_evidence_count INT NOT NULL DEFAULT 0
      AFTER adjustment_event_count
    """,
    """
    ALTER TABLE m2_daily_runs
      ADD COLUMN IF NOT EXISTS lineage_evidence_sha256 CHAR(64) NOT NULL
      DEFAULT '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
      AFTER adjustments_sha256
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


LINEAGE_UPSERT = """
INSERT INTO m2_daily_lineage_evidence (
  dataset_id, symbol, target_session, evidence_kind, source, evidence_json, row_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  target_session=VALUES(target_session), source=VALUES(source),
  evidence_json=VALUES(evidence_json), row_sha256=VALUES(row_sha256)
"""


CHECKPOINT_UPSERT = """
INSERT INTO m2_daily_symbol_checkpoints (
  dataset_id, symbol, target_session, status, primary_present, adjusted_present,
  tradeability_present, verification_required, verification_present,
  reported_previous_close, status_source, reported_previous_close_source,
  checkpoint_origin_dataset_id,
  primary_sha256, adjusted_sha256, tradeability_sha256,
  verification_sha256, adjustments_sha256, lineage_evidence_sha256,
  error_class, error_message
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
  target_session=VALUES(target_session), status=VALUES(status),
  primary_present=VALUES(primary_present), adjusted_present=VALUES(adjusted_present),
  tradeability_present=VALUES(tradeability_present),
  verification_required=VALUES(verification_required), verification_present=VALUES(verification_present),
  reported_previous_close=VALUES(reported_previous_close), status_source=VALUES(status_source),
  reported_previous_close_source=VALUES(reported_previous_close_source),
  checkpoint_origin_dataset_id=VALUES(checkpoint_origin_dataset_id),
  primary_sha256=VALUES(primary_sha256),
  adjusted_sha256=VALUES(adjusted_sha256), tradeability_sha256=VALUES(tradeability_sha256),
  verification_sha256=VALUES(verification_sha256), adjustments_sha256=VALUES(adjustments_sha256),
  lineage_evidence_sha256=VALUES(lineage_evidence_sha256),
  error_class=VALUES(error_class), error_message=VALUES(error_message)
"""


RUN_INSERT = """
INSERT INTO m2_daily_runs (
  dataset_id, schema_version, manifest_version, target_session, previous_session,
  snapshot_effective_session, base_history_dataset_id, predecessor_dataset_id,
  scope_sha256, authoritative, simulation_orders_allowed, accepted,
  expected_symbol_count, primary_row_count, adjusted_row_count,
  tradeability_row_count, verification_row_count, adjustment_event_count, lineage_evidence_count,
  primary_sha256, adjusted_sha256, tradeability_sha256, verification_sha256,
  adjustments_sha256, lineage_evidence_sha256, quality_sha256, manifest_sha256, manifest_json
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    canonical_rows = [_canonical_adjusted_bar(row) for row in rows]
    return [(
        dataset_id, row["symbol"], row["business_date"], row["exchange"], row["index_code"],
        row["open"], row["high"], row["low"], row["close"], row["previous_close"],
        int(row["volume_shares"]), row["amount_cny"], row.get("turnover_percent"),
        row["qfq_factor"], row["hfq_factor"], row["qfq_open"], row["qfq_high"],
        row["qfq_low"], row["qfq_close"], row["hfq_open"], row["hfq_high"],
        row["hfq_low"], row["hfq_close"], row["primary_source"], row["factor_source"],
        row["schema_version"], sha256(row),
    ) for row in canonical_rows]


def _fact_rows(dataset_id: str, rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    canonical_rows = [_canonical_tradeability_fact(row) for row in rows]
    return [(
        dataset_id, row["symbol"], row["business_date"], row["index_code"],
        _bool(row["has_primary_bar"]), _bool(row["has_secondary_status"]),
        _bool(row["is_suspended"]), _bool(row.get("is_st")), int(row["listing_age_sessions"]),
        row.get("limit_rate"), row.get("limit_up"), row.get("limit_down"),
        _bool(row["at_limit_up"]), _bool(row["at_limit_down"]),
        _bool(row["one_price_limit_up"]), _bool(row["one_price_limit_down"]),
        _bool(row["can_buy"]), _bool(row["can_sell"]), _compact(row["block_reasons"]),
        row["schema_version"], sha256(row),
    ) for row in canonical_rows]


def _event_rows(dataset_id: str, rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [(
        dataset_id, row["symbol"], row["effective_date"], row["qfq_factor"],
        row["hfq_factor"], row["source"], sha256(row),
    ) for row in rows]


def _lineage_rows(dataset_id: str, rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    canonical_rows = [canonical_lineage_evidence(row) for row in rows]
    return [(
        dataset_id, row["symbol"], row["target_session"], row["kind"], row["source"],
        _compact(row), sha256(row),
    ) for row in canonical_rows]


def _symbols(evidence: DailyEvidence) -> set[str]:
    return {
        str(row["symbol"])
        for collection in (
            evidence.primary_bars, evidence.tradeability, evidence.verification_bars,
            evidence.adjusted_bars, evidence.adjustments,
            evidence.lineage_evidence,
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
    adjusted_rows = [
        _canonical_adjusted_bar(row)
        for row in evidence.adjusted_bars if row.get("symbol") == symbol
    ]
    fact_rows = [
        _canonical_tradeability_fact(row)
        for row in evidence.tradeability if row.get("symbol") == symbol
    ]
    verification_rows = [row for row in evidence.verification_bars if row.get("symbol") == symbol]
    event_rows = [row for row in evidence.adjustments if row.get("symbol") == symbol]
    lineage_rows = sorted(
        (
            canonical_lineage_evidence(row)
            for row in evidence.lineage_evidence if row.get("symbol") == symbol
        ),
        key=lambda row: (row["symbol"], row["kind"]),
    )
    if len(primary_rows) > 1 or len(adjusted_rows) > 1 or len(fact_rows) > 1 or len(verification_rows) > 1:
        raise ValueError("daily symbol checkpoint permits at most one row per evidence type")
    if status == "succeeded" and len(fact_rows) != 1:
        raise ValueError("succeeded daily checkpoint requires a tradeability fact")
    if primary_rows and len(adjusted_rows) != 1:
        raise ValueError("a stored daily primary bar requires one adjusted bar")
    if verification_required and status == "succeeded" and primary_rows and len(verification_rows) != 1:
        raise ValueError("succeeded verification target requires an independent verification row")
    lineage_keys = {(row["symbol"], row["kind"]) for row in lineage_rows}
    if len(lineage_keys) != len(lineage_rows):
        raise ValueError("daily symbol checkpoint contains duplicate lineage evidence")

    with connection.cursor() as cursor:
        cursor.execute("SELECT accepted FROM m2_daily_runs WHERE dataset_id=%s", (dataset_id,))
        accepted_rows = cursor.fetchall()
        if accepted_rows and bool(accepted_rows[0][0]):
            raise RuntimeError(f"accepted daily dataset is immutable: {dataset_id}")
        for table in (
            "m2_daily_primary_bars", "m2_daily_adjusted_bars",
            "m2_daily_tradeability_facts", "m2_daily_verification_bars",
            "m2_daily_adjustment_events", "m2_daily_lineage_evidence",
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
        "lineage_evidence": _upsert_many(connection, LINEAGE_UPSERT, _lineage_rows(dataset_id, lineage_rows)),
    }
    checkpoint = (
        dataset_id, symbol, target_session.isoformat(), status, int(bool(primary_rows)),
        int(bool(adjusted_rows)), int(bool(fact_rows)), int(verification_required),
        int(bool(verification_rows)), _decimal(reported_previous_close),
        evidence.manifest.get("status_source"), evidence.manifest.get("previous_close_source"),
        evidence.manifest.get("checkpoint_origin_dataset_id"),
        sha256(primary_rows) if primary_rows else None,
        sha256(adjusted_rows) if adjusted_rows else None,
        sha256(fact_rows) if fact_rows else None,
        sha256(verification_rows) if verification_rows else None,
        sha256(event_rows) if event_rows else None,
        sha256(lineage_rows) if lineage_rows else None,
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
        SELECT symbol, status, verification_required, reported_previous_close,
               status_source, reported_previous_close_source, error_class, error_message,
               checkpoint_origin_dataset_id,
               primary_sha256, adjusted_sha256, tradeability_sha256,
               verification_sha256, adjustments_sha256, lineage_evidence_sha256
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
        "status_sources": {
            str(row[0]): str(row[4]) for row in checkpoints if row[4] is not None
        },
        "reported_previous_close_sources": {
            str(row[0]): str(row[5]) for row in checkpoints if row[5] is not None
        },
        "checkpoint_origin_dataset_ids": {
            str(row[0]): str(row[8]) for row in checkpoints if row[8] is not None
        },
        "errors": {
            str(row[0]): f"{row[6] or 'RuntimeError'}: {row[7] or 'incomplete daily evidence'}"
            for row in checkpoints if row[6] or row[7]
        },
    }
    if not retained:
        return DailyEvidence(
            manifest={"authoritative": False, "simulation_orders_allowed": False},
            primary_bars=[], tradeability=[], verification_bars=[], adjusted_bars=[], adjustments=[],
            lineage_evidence=[],
        ), metadata

    def keep(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [row for row in rows if str(row[0]) in retained]

    primary = keep(_query_all(connection, """
        SELECT symbol, business_date, source, exchange, open_price, high, low, close_price,
               previous_close, volume_shares, amount_cny, turnover_percent, trade_status,
               is_st, adjustment, source_schema_version, row_sha256
        FROM m2_daily_primary_bars
        WHERE dataset_id=%s ORDER BY source, symbol, business_date
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
        FROM m2_daily_verification_bars
        WHERE dataset_id=%s ORDER BY source, symbol, business_date
    """, (dataset_id,)))
    events = keep(_query_all(connection, """
        SELECT symbol, effective_date, qfq_factor, hfq_factor, source, row_sha256
        FROM m2_daily_adjustment_events WHERE dataset_id=%s ORDER BY symbol, effective_date
    """, (dataset_id,)))
    lineage = keep(_query_all(connection, """
        SELECT symbol, target_session, evidence_kind, source, evidence_json, row_sha256
        FROM m2_daily_lineage_evidence WHERE dataset_id=%s ORDER BY symbol, evidence_kind
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
    canonical_adjusted = [_canonical_adjusted_bar({
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
    }) for row in adjusted]
    canonical_facts = [_canonical_tradeability_fact({
        "symbol": str(row[0]), "business_date": _date_text(row[1]), "index_code": row[2],
        "has_primary_bar": bool(row[3]), "has_secondary_status": bool(row[4]),
        "is_suspended": bool(row[5]), "is_st": None if row[6] is None else bool(row[6]),
        "listing_age_sessions": int(row[7]), "limit_rate": row[8],
        "limit_up": row[9], "limit_down": row[10],
        "at_limit_up": bool(row[11]), "at_limit_down": bool(row[12]),
        "one_price_limit_up": bool(row[13]), "one_price_limit_down": bool(row[14]),
        "can_buy": bool(row[15]), "can_sell": bool(row[16]),
        "block_reasons": json.loads(row[17]), "schema_version": row[18],
    }) for row in facts]
    canonical_verification = [daily_bar(row) for row in verification]
    canonical_events = [{
        "symbol": str(row[0]), "effective_date": _date_text(row[1]),
        "qfq_factor": _decimal(row[2]), "hfq_factor": _decimal(row[3]), "source": row[4],
    } for row in events]
    canonical_lineage = [canonical_lineage_evidence(json.loads(str(row[4]))) for row in lineage]

    for label, raw_rows, canonical_rows in (
        ("primary", primary, canonical_primary),
        ("adjusted", adjusted, canonical_adjusted),
        ("tradeability", facts, canonical_facts),
        ("verification", verification, canonical_verification),
        ("adjustments", events, canonical_events),
        ("lineage_evidence", lineage, canonical_lineage),
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
        lineage_evidence=canonical_lineage,
    )
    collections = {
        "primary": evidence.primary_bars,
        "adjusted": evidence.adjusted_bars,
        "tradeability": evidence.tradeability,
        "verification": evidence.verification_bars,
        "adjustments": evidence.adjustments,
        "lineage_evidence": evidence.lineage_evidence,
    }
    checkpoint_hash_positions = {
        "primary": 9, "adjusted": 10, "tradeability": 11,
        "verification": 12, "adjustments": 13, "lineage_evidence": 14,
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


def _publish_recovered_checkpoints_batch(
    connection: Any,
    *,
    dataset_id: str,
    target_session: date,
    verification_symbols: set[str],
    choices: Mapping[
        str,
        tuple[tuple[int, int, str], str, DailyEvidence, Decimal | None, str, str],
    ],
) -> None:
    """Publish validated missing checkpoints in one transaction."""
    if not choices:
        return
    symbols = sorted(choices)
    placeholders = ",".join(["%s"] * len(symbols))
    primary_rows: list[Mapping[str, Any]] = []
    adjusted_rows: list[Mapping[str, Any]] = []
    fact_rows: list[Mapping[str, Any]] = []
    verification_rows: list[Mapping[str, Any]] = []
    event_rows: list[Mapping[str, Any]] = []
    lineage_rows: list[Mapping[str, Any]] = []
    checkpoint_rows: list[tuple[Any, ...]] = []
    for symbol in symbols:
        _rank, candidate_id, evidence, reported, status_source, previous_source = choices[symbol]
        canonical_facts = [_canonical_tradeability_fact(row) for row in evidence.tradeability]
        primary_rows.extend(evidence.primary_bars)
        canonical_adjusted = [_canonical_adjusted_bar(row) for row in evidence.adjusted_bars]
        adjusted_rows.extend(canonical_adjusted)
        fact_rows.extend(canonical_facts)
        verification_rows.extend(evidence.verification_bars)
        event_rows.extend(evidence.adjustments)
        canonical_lineage = sorted(
            (canonical_lineage_evidence(row) for row in evidence.lineage_evidence),
            key=lambda row: (row["symbol"], row["kind"]),
        )
        lineage_rows.extend(canonical_lineage)
        checkpoint_rows.append((
            dataset_id, symbol, target_session.isoformat(), "succeeded",
            int(bool(evidence.primary_bars)), int(bool(canonical_adjusted)),
            int(bool(canonical_facts)), int(symbol in verification_symbols),
            int(bool(evidence.verification_bars)), _decimal(reported),
            status_source, previous_source, candidate_id,
            sha256(evidence.primary_bars) if evidence.primary_bars else None,
            sha256(canonical_adjusted) if canonical_adjusted else None,
            sha256(canonical_facts) if canonical_facts else None,
            sha256(evidence.verification_bars) if evidence.verification_bars else None,
            sha256(evidence.adjustments) if evidence.adjustments else None,
            sha256(canonical_lineage) if canonical_lineage else None,
            None, None,
        ))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT accepted FROM m2_daily_runs WHERE dataset_id=%s", (dataset_id,))
            accepted_rows = cursor.fetchall()
            if accepted_rows and bool(accepted_rows[0][0]):
                raise RuntimeError(f"accepted daily dataset is immutable: {dataset_id}")
            for table in (
                "m2_daily_primary_bars", "m2_daily_adjusted_bars",
                "m2_daily_tradeability_facts", "m2_daily_verification_bars",
                "m2_daily_adjustment_events", "m2_daily_lineage_evidence",
            ):
                cursor.execute(
                    f"DELETE FROM {table} WHERE dataset_id=%s AND symbol IN ({placeholders})",
                    (dataset_id, *symbols),
                )
        _upsert_many(connection, PRIMARY_UPSERT, _primary_rows(dataset_id, primary_rows))
        _upsert_many(connection, ADJUSTED_UPSERT, _adjusted_rows(dataset_id, adjusted_rows))
        _upsert_many(connection, TRADEABILITY_UPSERT, _fact_rows(dataset_id, fact_rows))
        _upsert_many(connection, VERIFICATION_UPSERT, _primary_rows(dataset_id, verification_rows))
        _upsert_many(connection, ADJUSTMENT_UPSERT, _event_rows(dataset_id, event_rows))
        _upsert_many(connection, LINEAGE_UPSERT, _lineage_rows(dataset_id, lineage_rows))
        _upsert_many(connection, CHECKPOINT_UPSERT, checkpoint_rows)
        connection.commit()
    except BaseException:
        if hasattr(connection, "rollback"):
            connection.rollback()
        raise


def _recovered_lineage_matches(
    *,
    symbol: str,
    target_session: date,
    index_code: str,
    evidence: DailyEvidence,
    reported_previous_close: Decimal | None,
    previous_states: Mapping[str, PreviousAdjustedState],
) -> bool:
    """Rebuild one candidate adjusted row against the active predecessor."""
    if not evidence.primary_bars:
        return not evidence.adjusted_bars
    state = previous_states.get(symbol)
    if state is None or reported_previous_close is None:
        return False
    row = evidence.primary_bars[0]
    primary = DailyBar(
        source=str(row["source"]), symbol=str(row["symbol"]), exchange=str(row["exchange"]),
        business_date=parse_date(row["business_date"]), open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])), low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        previous_close=None if row.get("previous_close") is None else Decimal(str(row["previous_close"])),
        volume_shares=int(row["volume_shares"]), amount_cny=Decimal(str(row["amount_cny"])),
        turnover_percent=(
            None if row.get("turnover_percent") is None
            else Decimal(str(row["turnover_percent"]))
        ),
        trade_status=str(row["trade_status"]), is_st=row.get("is_st"),
        adjustment=str(row.get("adjustment", "none")), schema_version=str(row["schema_version"]),
    )
    events = [
        AdjustmentEvent(
            symbol=str(item["symbol"]), effective_date=parse_date(item["effective_date"]),
            qfq_factor=Decimal(str(item["qfq_factor"])),
            hfq_factor=Decimal(str(item["hfq_factor"])), source=str(item["source"]),
        )
        for item in evidence.adjustments
    ]
    try:
        rebuilt = build_daily_adjusted_bars(
            target_session=target_session,
            previous_session=state.business_date,
            membership={symbol: index_code},
            primary_bars=[primary],
            previous_states={symbol: state},
            reported_previous_closes={symbol: reported_previous_close},
            adjustment_events=events,
        )
    except (RuntimeError, ValueError):
        return False
    return len(rebuilt) == 1 and rebuilt[0].canonical() == evidence.adjusted_bars[0]


def _daily_bar_from_canonical(row: Mapping[str, Any]) -> DailyBar:
    return DailyBar(
        source=str(row["source"]), symbol=str(row["symbol"]), exchange=str(row["exchange"]),
        business_date=parse_date(row["business_date"]), open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])), low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        previous_close=None if row.get("previous_close") is None else Decimal(str(row["previous_close"])),
        volume_shares=int(row["volume_shares"]), amount_cny=Decimal(str(row["amount_cny"])),
        turnover_percent=(
            None if row.get("turnover_percent") is None else Decimal(str(row["turnover_percent"]))
        ),
        trade_status=str(row["trade_status"]), is_st=row.get("is_st"),
        adjustment=str(row.get("adjustment", "none")), schema_version=str(row["schema_version"]),
    )


def _recovered_verification_matches(
    primary: list[dict[str, Any]],
    verification: list[dict[str, Any]],
) -> bool:
    if not primary:
        return not verification
    if len(primary) != 1 or len(verification) != 1:
        return False
    return not cross_source_consistency_errors(
        _daily_bar_from_canonical(primary[0]),
        _daily_bar_from_canonical(verification[0]),
    )


def recover_compatible_daily_checkpoints(
    connection: Any,
    *,
    dataset_id: str,
    target_session: date,
    expected_membership: Mapping[str, str],
    verification_symbols: Iterable[str],
    previous_states: Mapping[str, PreviousAdjustedState],
    existing_metadata: Mapping[str, Any] | None = None,
    excluded_symbols: Iterable[str] = (),
) -> dict[str, Any]:
    """Copy only validated successful evidence into a stable daily namespace.

    Earlier unaccepted retries may have used observation-dependent dataset ids.
    Their rows remain untouched.  This function validates every source dataset
    through normal hash readback, chooses a deterministic preferred source per
    symbol, and republishes the selected evidence through the ordinary atomic
    checkpoint writer.
    """
    expected = dict(expected_membership)
    required_verification = set(verification_symbols)
    if existing_metadata is None:
        _existing, loaded_metadata = load_daily_checkpoint_evidence(connection, dataset_id)
    else:
        loaded_metadata = existing_metadata
    already_present = set(loaded_metadata["succeeded_symbols"])
    excluded = {normalize_symbol(symbol) for symbol in excluded_symbols}
    if not excluded <= set(expected):
        raise ValueError("excluded recovery symbols must be inside the expected membership")
    missing_symbols = sorted(set(expected) - already_present - excluded)
    if not missing_symbols:
        return {
            "already_present": len(already_present),
            "recovered": 0,
            "candidate_datasets": 0,
            "recovered_by_source_dataset": {},
            "rejected_datasets": {},
        }
    placeholders = ",".join(["%s"] * len(missing_symbols))
    candidate_rows = _query_all(connection, f"""
        SELECT DISTINCT checkpoint.dataset_id
        FROM m2_daily_symbol_checkpoints AS checkpoint
        LEFT JOIN m2_daily_runs AS aggregate ON aggregate.dataset_id=checkpoint.dataset_id
        WHERE checkpoint.target_session=%s AND checkpoint.dataset_id<>%s
          AND checkpoint.status='succeeded'
          AND checkpoint.symbol IN ({placeholders})
          AND (aggregate.accepted IS NULL OR aggregate.accepted=0)
        ORDER BY checkpoint.dataset_id
    """, (target_session.isoformat(), dataset_id, *missing_symbols))

    source_rank = {
        "akshare_eastmoney": 0,
        "akshare_sina": 1,
        "tencent_archive": 2,
    }
    choices: dict[str, tuple[tuple[int, int, str], str, DailyEvidence, Decimal | None, str, str]] = {}
    rejected_datasets: dict[str, str] = {}
    for row in candidate_rows:
        candidate_id = str(row[0])
        try:
            evidence, metadata = load_daily_checkpoint_evidence(connection, candidate_id)
        except Exception as error:
            rejected_datasets[candidate_id] = f"{type(error).__name__}: {error}"
            continue
        succeeded = set(metadata["succeeded_symbols"])
        # Candidate datasets may contain a mixture of eligible and explicitly
        # excluded symbols. Only copy the precomputed missing set; otherwise
        # an excluded corporate-action candidate can leak back in with the
        # eligible symbols from the same source dataset.
        for symbol in sorted(succeeded & set(missing_symbols)):
            primary = [item for item in evidence.primary_bars if item["symbol"] == symbol]
            adjusted = [item for item in evidence.adjusted_bars if item["symbol"] == symbol]
            facts = [item for item in evidence.tradeability if item["symbol"] == symbol]
            verification = [item for item in evidence.verification_bars if item["symbol"] == symbol]
            events = [item for item in evidence.adjustments if item["symbol"] == symbol]
            lineage = [item for item in evidence.lineage_evidence if item["symbol"] == symbol]
            if len(facts) != 1 or facts[0]["index_code"] != expected[symbol]:
                continue
            if len(primary) > 1 or len(adjusted) > 1 or len(verification) > 1:
                continue
            if bool(primary) != bool(adjusted):
                continue
            if symbol in required_verification and primary and len(verification) != 1:
                continue
            if symbol in required_verification and not _recovered_verification_matches(primary, verification):
                continue
            if not primary and not (facts[0]["has_secondary_status"] and facts[0]["is_suspended"]):
                continue
            reported = metadata["reported_previous_closes"].get(symbol)
            if primary and reported is None:
                continue
            selected_evidence = DailyEvidence(
                manifest={"authoritative": False, "simulation_orders_allowed": False},
                primary_bars=primary,
                adjusted_bars=adjusted,
                tradeability=facts,
                verification_bars=verification,
                adjustments=events,
                lineage_evidence=lineage,
            )
            if not _recovered_lineage_matches(
                symbol=symbol,
                target_session=target_session,
                index_code=expected[symbol],
                evidence=selected_evidence,
                reported_previous_close=reported,
                previous_states=previous_states,
            ):
                continue
            status_source = metadata["status_sources"].get(symbol, "legacy_baostock_daily_status")
            previous_source = metadata["reported_previous_close_sources"].get(
                symbol,
                "legacy_baostock_reported_preclose" if reported is not None else "unavailable",
            )
            selected = DailyEvidence(
                manifest={
                    "authoritative": False,
                    "simulation_orders_allowed": False,
                    "status_source": status_source,
                    "previous_close_source": previous_source,
                    "checkpoint_origin_dataset_id": candidate_id,
                },
                primary_bars=selected_evidence.primary_bars,
                adjusted_bars=selected_evidence.adjusted_bars,
                tradeability=selected_evidence.tradeability,
                verification_bars=selected_evidence.verification_bars,
                adjustments=selected_evidence.adjustments,
                lineage_evidence=selected_evidence.lineage_evidence,
            )
            primary_name = str(primary[0]["source"]) if primary else ""
            rank = (source_rank.get(primary_name, 99), -len(succeeded), candidate_id)
            previous_choice = choices.get(symbol)
            if previous_choice is None or rank < previous_choice[0]:
                choices[symbol] = (
                    rank, candidate_id, selected, reported, status_source, previous_source,
                )

    recovered_by_source: dict[str, int] = {}
    for symbol in sorted(choices):
        _rank, candidate_id, _selected, _reported, _status_source, _previous_source = choices[symbol]
        recovered_by_source[candidate_id] = recovered_by_source.get(candidate_id, 0) + 1
    _publish_recovered_checkpoints_batch(
        connection,
        dataset_id=dataset_id,
        target_session=target_session,
        verification_symbols=required_verification,
        choices=choices,
    )
    return {
        "already_present": len(already_present),
        "recovered": len(choices),
        "candidate_datasets": len(candidate_rows),
        "recovered_by_source_dataset": dict(sorted(recovered_by_source.items())),
        "rejected_datasets": rejected_datasets,
    }


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


def load_latest_prior_adjusted_states(
    connection: Any,
    *,
    base_history_dataset_id: str,
    previous_session: date,
    symbols: Iterable[str],
) -> dict[str, PreviousAdjustedState]:
    """Load the latest immutable state before a missing exact predecessor."""
    requested = tuple(sorted({normalize_symbol(symbol) for symbol in symbols}))
    if not requested:
        return {}
    placeholders = ",".join(["%s"] * len(requested))
    daily_rows = _query_all(connection, f"""
        SELECT b.symbol, b.business_date, b.close_price, b.qfq_factor, b.hfq_factor,
               b.dataset_id
        FROM m2_daily_adjusted_bars b
        JOIN m2_daily_runs r ON r.dataset_id=b.dataset_id
        WHERE r.base_history_dataset_id=%s AND r.accepted=1
          AND r.authoritative=0 AND r.simulation_orders_allowed=0
          AND b.business_date<%s AND b.symbol IN ({placeholders})
        ORDER BY b.symbol, b.business_date DESC
    """, (base_history_dataset_id, previous_session.isoformat(), *requested))
    history_rows = _query_all(connection, f"""
        SELECT b.symbol, b.business_date, b.close_price, b.qfq_factor, b.hfq_factor,
               %s AS source_dataset_id
        FROM m2_history_run_shards s
        JOIN m2_historical_bars b ON b.dataset_id=s.shard_dataset_id
        WHERE s.merged_dataset_id=%s AND b.business_date<%s
          AND b.symbol IN ({placeholders})
        ORDER BY b.symbol, b.business_date DESC
    """, (
        base_history_dataset_id, base_history_dataset_id, previous_session.isoformat(), *requested,
    ))
    candidates: dict[str, PreviousAdjustedState] = {}
    for row in [*daily_rows, *history_rows]:
        symbol = str(row[0])
        candidate = PreviousAdjustedState(
            symbol=symbol,
            business_date=date.fromisoformat(_date_text(row[1])),
            raw_close=Decimal(str(row[2])),
            qfq_factor=Decimal(str(row[3])),
            hfq_factor=Decimal(str(row[4])),
            source_dataset_id=str(row[5]),
        )
        existing = candidates.get(symbol)
        if existing is None or candidate.business_date > existing.business_date:
            candidates[symbol] = candidate
    return candidates


def recovered_previous_states_from_lineage(
    rows: Iterable[Mapping[str, Any]],
    *,
    previous_session: date,
) -> dict[str, PreviousAdjustedState]:
    """Rehydrate exact predecessor states from hash-verified V4 evidence."""
    states: dict[str, PreviousAdjustedState] = {}
    for raw in rows:
        row = canonical_lineage_evidence(raw)
        if row["kind"] != "gap_no_adjustment_recovery":
            continue
        details = row["details"]
        recovered_session = parse_date(details.get("recovered_session"))
        if recovered_session != previous_session:
            raise RuntimeError(
                f"daily lineage recovery has the wrong predecessor session for {row['symbol']}"
            )
        state = PreviousAdjustedState(
            symbol=row["symbol"],
            business_date=recovered_session,
            raw_close=Decimal(str(details["recovered_raw_close"])),
            qfq_factor=Decimal(str(details["qfq_factor"])),
            hfq_factor=Decimal(str(details["hfq_factor"])),
            source_dataset_id=f"daily-lineage:{sha256(row)}",
        )
        if state.symbol in states and states[state.symbol] != state:
            raise RuntimeError(f"conflicting daily lineage recoveries for {state.symbol}")
        states[state.symbol] = state
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
        SELECT run.dataset_id, run.target_session, run.previous_session, run.predecessor_dataset_id,
               run.authoritative, run.simulation_orders_allowed
        FROM m2_daily_runs AS run
        LEFT JOIN m2_daily_run_supersessions AS supersession
          ON supersession.superseded_dataset_id=run.dataset_id
        WHERE run.base_history_dataset_id=%s AND run.accepted=1
          AND supersession.superseded_dataset_id IS NULL
        ORDER BY run.target_session
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


def daily_correction_context(
    connection: Any,
    *,
    base_history_dataset_id: str,
    superseded_dataset_id: str,
    target_session: date,
) -> tuple[date, str]:
    """Validate a correction of the active lineage tip without mutating it."""
    normalized_superseded_dataset_id = str(superseded_dataset_id).strip()
    if not normalized_superseded_dataset_id:
        raise RuntimeError("daily correction requires a non-empty superseded dataset id")
    active_session, active_dataset_id = latest_accepted_lineage(
        connection, base_history_dataset_id,
    )
    if active_session != target_session or active_dataset_id != normalized_superseded_dataset_id:
        raise RuntimeError(
            "daily correction is limited to the active lineage tip: "
            f"active tip {active_session.isoformat()}/{active_dataset_id!r} "
            f"(len={len(active_dataset_id)}); "
            f"requested {target_session.isoformat()}/{normalized_superseded_dataset_id!r} "
            f"(len={len(normalized_superseded_dataset_id)})"
        )
    rows = _query_all(connection, """
        SELECT run.target_session, run.previous_session, run.predecessor_dataset_id,
               run.base_history_dataset_id, run.accepted, run.authoritative,
               run.simulation_orders_allowed, supersession.superseded_dataset_id
        FROM m2_daily_runs AS run
        LEFT JOIN m2_daily_run_supersessions AS supersession
          ON supersession.superseded_dataset_id=run.dataset_id
        WHERE run.dataset_id=%s
    """, (normalized_superseded_dataset_id,))
    if len(rows) != 1:
        raise RuntimeError("superseded daily dataset does not exist")
    row = rows[0]
    recorded_target = date.fromisoformat(_date_text(row[0]))
    previous_session = date.fromisoformat(_date_text(row[1]))
    predecessor_dataset_id = str(row[2])
    if (
        recorded_target != target_session
        or str(row[3]) != base_history_dataset_id
        or not bool(row[4])
        or bool(row[5])
        or bool(row[6])
        or row[7] is not None
    ):
        raise RuntimeError("daily correction target is not the active accepted research-only result")
    downstream = _query_all(connection, """
        SELECT run.dataset_id
        FROM m2_daily_runs AS run
        LEFT JOIN m2_daily_run_supersessions AS supersession
          ON supersession.superseded_dataset_id=run.dataset_id
        WHERE run.base_history_dataset_id=%s AND run.accepted=1
          AND supersession.superseded_dataset_id IS NULL
          AND run.target_session>%s
        LIMIT 1
    """, (base_history_dataset_id, target_session.isoformat()))
    if downstream:
        raise RuntimeError("daily correction is limited to the active lineage tip")
    return previous_session, predecessor_dataset_id


def _manifest_hashes(evidence: DailyEvidence) -> dict[str, Any]:
    canonical_adjusted = [_canonical_adjusted_bar(row) for row in evidence.adjusted_bars]
    canonical_lineage = sorted(
        (canonical_lineage_evidence(row) for row in evidence.lineage_evidence),
        key=lambda row: (row["symbol"], row["kind"]),
    )
    return {
        "primary_row_count": len(evidence.primary_bars),
        "adjusted_row_count": len(evidence.adjusted_bars),
        "tradeability_row_count": len(evidence.tradeability),
        "verification_row_count": len(evidence.verification_bars),
        "adjustment_event_count": len(evidence.adjustments),
        "lineage_evidence_count": len(canonical_lineage),
        "primary_sha256": sha256(evidence.primary_bars),
        "adjusted_sha256": sha256(canonical_adjusted),
        "tradeability_sha256": sha256(evidence.tradeability),
        "verification_sha256": sha256(evidence.verification_bars),
        "adjustments_sha256": sha256(evidence.adjustments),
        "lineage_evidence_sha256": sha256(canonical_lineage),
    }


def publish_daily_run(
    connection: Any,
    evidence: DailyEvidence,
    *,
    dataset_id: str,
    base_history_dataset_id: str,
    predecessor_dataset_id: str,
    supersedes_dataset_id: str | None = None,
    correction_reason: str | None = None,
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
    if supersedes_dataset_id is not None:
        if (
            not correction_reason
            or manifest.get("supersedes_dataset_id") != supersedes_dataset_id
            or manifest.get("correction_reason") != correction_reason
        ):
            raise RuntimeError("daily correction metadata is missing or inconsistent")
        replay = _query_all(connection, """
            SELECT supersession.replacement_dataset_id, run.manifest_sha256
            FROM m2_daily_run_supersessions AS supersession
            JOIN m2_daily_runs AS run ON run.dataset_id=supersession.replacement_dataset_id
            WHERE supersession.superseded_dataset_id=%s
        """, (supersedes_dataset_id,))
        if replay:
            if len(replay) == 1 and str(replay[0][0]) == dataset_id and str(replay[0][1]) == manifest_hash:
                return {
                    "dataset_id": dataset_id,
                    "accepted": True,
                    "idempotent_replay": True,
                    "superseded_dataset_id": supersedes_dataset_id,
                }
            raise RuntimeError("daily correction target already has a different replacement")
    existing = _query_all(connection, """
        SELECT run.dataset_id, run.manifest_sha256
        FROM m2_daily_runs AS run
        LEFT JOIN m2_daily_run_supersessions AS supersession
          ON supersession.superseded_dataset_id=run.dataset_id
        WHERE run.target_session=%s AND run.accepted=1
          AND supersession.superseded_dataset_id IS NULL
    """, (target,))
    if existing:
        if supersedes_dataset_id is not None:
            if len(existing) != 1 or str(existing[0][0]) != supersedes_dataset_id:
                raise RuntimeError("daily correction does not name the active accepted result")
        elif len(existing) == 1 and str(existing[0][0]) == dataset_id and str(existing[0][1]) == manifest_hash:
            return {"dataset_id": dataset_id, "accepted": True, "idempotent_replay": True}
        else:
            raise RuntimeError(f"a different accepted daily result already exists for {target}")
    elif supersedes_dataset_id is not None:
        raise RuntimeError("daily correction target is no longer active")

    row = (
        dataset_id, DAILY_STORE_SCHEMA_VERSION, manifest["manifest_version"], target,
        manifest["previous_session"], manifest["snapshot_effective_session"],
        base_history_dataset_id, predecessor_dataset_id, scope_hash, 0, 0, 1,
        expected_symbols, expected["primary_row_count"], expected["adjusted_row_count"],
        expected["tradeability_row_count"], expected["verification_row_count"],
        expected["adjustment_event_count"], expected["lineage_evidence_count"],
        expected["primary_sha256"],
        expected["adjusted_sha256"], expected["tradeability_sha256"],
        expected["verification_sha256"], expected["adjustments_sha256"],
        expected["lineage_evidence_sha256"],
        manifest["quality_sha256"], manifest_hash, _compact(manifest),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(RUN_INSERT, row)
            if supersedes_dataset_id is not None:
                correction = {
                    "superseded_dataset_id": supersedes_dataset_id,
                    "replacement_dataset_id": dataset_id,
                    "target_session": target,
                    "reason_code": correction_reason,
                    "replacement_manifest_sha256": manifest_hash,
                }
                cursor.execute(
                    """INSERT INTO m2_daily_run_supersessions (
                         superseded_dataset_id, replacement_dataset_id, target_session,
                         reason_code, evidence_sha256, evidence_json
                       ) VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        supersedes_dataset_id, dataset_id, target, correction_reason,
                        sha256(correction), _compact(correction),
                    ),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    result = {"dataset_id": dataset_id, "accepted": True, "idempotent_replay": False}
    if supersedes_dataset_id is not None:
        result["superseded_dataset_id"] = supersedes_dataset_id
    return result


__all__ = [
    "DAILY_STORE_SCHEMA_VERSION", "DailyEvidence", "TiDBConfig", "connect",
    "daily_correction_context", "default_daily_dataset_id", "ensure_daily_schema", "latest_accepted_lineage",
    "load_base_references", "load_daily_checkpoint_evidence", "load_daily_evidence",
    "load_latest_prior_adjusted_states", "load_previous_adjusted_states",
    "recovered_previous_states_from_lineage", "publish_daily_run", "publish_daily_symbol_checkpoint",
]

"""Read a deliberately bounded, immutable M2 slice for RQAlpha acceptance.

The warehouse is queried once by a cloud preparation step.  RQAlpha receives a
small self-contained input file and never holds TiDB credentials.  This module
does not publish data, select a "latest" release, or admit adjusted vendor bars.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scripts.market_data.manifest import sha256


PINNED_HISTORY_DATASET_ID = (
    "m2-full-2026-07-24-"
    "993df9aab3cbd021a495535c9326eaa79f26f4bbfbe74b28215256e778e517f7-merged"
)
PINNED_DAILY_RELEASE_IDS = (
    "m2-daily-2026-07-27-"
    "041575afa0026e129426b2f4bbe4dc83915532c0e9a0fc387c631f8a562262a9",
    "m2-daily-2026-07-28-"
    "1392a4e46e59cd69fc330a36e81176070f18f27dde759a9376721411a3f7b851",
)
ACCEPTANCE_SYMBOLS = ("600519", "601866")
ACCEPTANCE_SESSIONS = (date(2026, 7, 24), date(2026, 7, 27), date(2026, 7, 28))
SCHEMA_VERSION = "m3-rqalpha-bounded-input-v1"


def _date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO date") from error


def _decimal(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise ValueError(f"{field} must be {'non-negative' if allow_zero else 'positive'} and finite")
    return result


def _bool(value: Any, field: str) -> bool:
    if value in (True, 1):
        return True
    if value in (False, 0):
        return False
    raise ValueError(f"{field} must be an explicit boolean")


@dataclass(frozen=True)
class M2BoundedResearchInput:
    history_dataset_id: str
    daily_release_ids: tuple[str, ...]
    sessions: tuple[date, ...]
    symbols: tuple[str, ...]
    instruments: tuple[Mapping[str, Any], ...]
    bars: tuple[Mapping[str, Any], ...]
    tradeability: tuple[Mapping[str, Any], ...]
    source_manifest_sha256s: tuple[str, ...]
    authoritative: bool = False
    simulation_orders_allowed: bool = False
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported bounded M2 input schema")
        if self.history_dataset_id != PINNED_HISTORY_DATASET_ID:
            raise ValueError("historical release is not explicitly pinned")
        if self.daily_release_ids != PINNED_DAILY_RELEASE_IDS:
            raise ValueError("daily releases are not explicitly pinned in order")
        if self.authoritative or self.simulation_orders_allowed:
            raise ValueError("M2 acceptance input must remain research-only")
        if self.sessions != ACCEPTANCE_SESSIONS or self.symbols != ACCEPTANCE_SYMBOLS:
            raise ValueError("M3.3 acceptance scope may not expand implicitly")
        if len(self.source_manifest_sha256s) != 3 or any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.source_manifest_sha256s
        ):
            raise ValueError("every source release requires an immutable manifest hash")

        instruments = {str(row.get("symbol")): row for row in self.instruments}
        if tuple(sorted(instruments)) != self.symbols or len(instruments) != len(self.instruments):
            raise ValueError("instrument scope is incomplete or duplicated")
        for symbol, row in instruments.items():
            if str(row.get("exchange")) not in {"SSE", "SZSE"}:
                raise ValueError(f"unsupported exchange for {symbol}")
            _date(row.get("ipo_date"), "ipo_date")

        expected = {(symbol, session) for session in self.sessions for symbol in self.symbols}
        bar_keys: set[tuple[str, date]] = set()
        for row in self.bars:
            key = (str(row.get("symbol")), _date(row.get("business_date"), "bar business_date"))
            if key not in expected or key in bar_keys:
                raise ValueError("bar is duplicated or outside the bounded scope")
            if str(row.get("adjustment")) != "none":
                raise ValueError("RQAlpha must receive raw unadjusted M2 bars")
            open_price = _decimal(row.get("open"), "open")
            high = _decimal(row.get("high"), "high")
            low = _decimal(row.get("low"), "low")
            close = _decimal(row.get("close"), "close")
            _decimal(row.get("previous_close"), "previous_close")
            if str(row.get("previous_close_origin")) not in {"stored_m2_raw", "prior_admitted_raw_close"}:
                raise ValueError("previous_close requires explicit admitted lineage")
            if not low <= min(open_price, close) <= max(open_price, close) <= high:
                raise ValueError("M2 OHLC values do not reconcile")
            _decimal(row.get("volume"), "volume", allow_zero=True)
            _decimal(row.get("total_turnover"), "total_turnover", allow_zero=True)
            for field in ("source_bar_sha256", "source_tradeability_sha256"):
                value = str(row.get(field, ""))
                if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                    raise ValueError(f"{field} requires M2 row lineage")
            bar_keys.add(key)

        facts: dict[tuple[str, date], Mapping[str, Any]] = {}
        for row in self.tradeability:
            key = (str(row.get("symbol")), _date(row.get("business_date"), "fact business_date"))
            if key not in expected or key in facts:
                raise ValueError("tradeability fact is duplicated or outside the bounded scope")
            has_bar = _bool(row.get("has_primary_bar"), "has_primary_bar")
            suspended = _bool(row.get("is_suspended"), "is_suspended")
            can_buy = _bool(row.get("can_buy"), "can_buy")
            can_sell = _bool(row.get("can_sell"), "can_sell")
            limit_flags = tuple(_bool(row.get(field), field) for field in (
                "at_limit_up", "at_limit_down", "one_price_limit_up", "one_price_limit_down"
            ))
            if has_bar != (key in bar_keys):
                raise ValueError("raw bar and tradeability fact do not reconcile")
            if (not has_bar or suspended) and (can_buy or can_sell):
                raise ValueError("missing or suspended data must fail closed")
            for field in ("limit_up", "limit_down"):
                _decimal(row.get(field), field)
            if str(row.get("price_limit_origin")) not in {
                "stored_m2_fact", "m2_confirmed_non_limit_acceptance_sentinel_v1",
                "blocked_non_actionable_sentinel_v1",
            }:
                raise ValueError("price limits require an explicit bounded rule or stored fact")
            if str(row.get("price_limit_origin")) == "m2_confirmed_non_limit_acceptance_sentinel_v1" and (
                not can_buy or not can_sell or any(limit_flags)
            ):
                raise ValueError("non-limit sentinel requires explicit M2 non-limit tradeability")
            if str(row.get("price_limit_origin")) == "blocked_non_actionable_sentinel_v1" and (
                can_buy or can_sell
            ):
                raise ValueError("blocked sentinel may never authorize a decision")
            facts[key] = row
        if set(facts) != expected:
            raise ValueError("bounded tradeability coverage is incomplete")
        if bar_keys != expected:
            raise ValueError("M3.3 admitted fixture requires complete raw-bar coverage")
        actionable = {
            key for key, row in facts.items()
            if row.get("can_buy") is True and row.get("can_sell") is True
        }
        if actionable != {("600519", date(2026, 7, 27))}:
            raise ValueError("M3.3 actionable fixture scope changed; review is required")

    @property
    def input_sha256(self) -> str:
        return sha256(self.to_mapping(include_hash=False))

    def to_mapping(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "history_dataset_id": self.history_dataset_id,
            "daily_release_ids": list(self.daily_release_ids),
            "sessions": [value.isoformat() for value in self.sessions],
            "symbols": list(self.symbols),
            "instruments": [dict(row) for row in self.instruments],
            "bars": [dict(row) for row in self.bars],
            "tradeability": [dict(row) for row in self.tradeability],
            "source_manifest_sha256s": list(self.source_manifest_sha256s),
            "authoritative": self.authoritative,
            "simulation_orders_allowed": self.simulation_orders_allowed,
        }
        if include_hash:
            result["input_sha256"] = self.input_sha256
        return result

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "M2BoundedResearchInput":
        result = cls(
            history_dataset_id=str(raw.get("history_dataset_id", "")),
            daily_release_ids=tuple(str(value) for value in raw.get("daily_release_ids", [])),
            sessions=tuple(_date(value, "session") for value in raw.get("sessions", [])),
            symbols=tuple(str(value) for value in raw.get("symbols", [])),
            instruments=tuple(dict(row) for row in raw.get("instruments", [])),
            bars=tuple(dict(row) for row in raw.get("bars", [])),
            tradeability=tuple(dict(row) for row in raw.get("tradeability", [])),
            source_manifest_sha256s=tuple(str(value) for value in raw.get("source_manifest_sha256s", [])),
            authoritative=_bool(raw.get("authoritative"), "authoritative"),
            simulation_orders_allowed=_bool(
                raw.get("simulation_orders_allowed"), "simulation_orders_allowed"
            ),
            schema_version=str(raw.get("schema_version", "")),
        )
        declared = str(raw.get("input_sha256", ""))
        if declared and declared != result.input_sha256:
            raise ValueError("bounded M2 input hash does not reconcile")
        return result


def load_bounded_input(path: Path) -> M2BoundedResearchInput:
    return M2BoundedResearchInput.from_mapping(json.loads(path.read_text(encoding="utf-8")))


def write_bounded_input(path: Path, value: M2BoundedResearchInput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value.to_mapping(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _query(connection: Any, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return list(cursor.fetchall())


def extract_pinned_acceptance_input(connection: Any) -> M2BoundedResearchInput:
    """Read only six bars/facts after verifying all three immutable run boundaries."""

    history_run = _query(
        connection,
        "SELECT accepted, authoritative, simulation_orders_allowed, manifest_sha256 "
        "FROM m2_history_runs WHERE dataset_id=%s",
        (PINNED_HISTORY_DATASET_ID,),
    )
    daily_runs = _query(
        connection,
        "SELECT dataset_id, accepted, authoritative, simulation_orders_allowed, manifest_sha256 "
        "FROM m2_daily_runs WHERE dataset_id IN (%s,%s) ORDER BY target_session",
        PINNED_DAILY_RELEASE_IDS,
    )
    boundaries = history_run + [row[1:] for row in daily_runs]
    if len(history_run) != 1 or [row[0] for row in daily_runs] != list(PINNED_DAILY_RELEASE_IDS):
        raise RuntimeError("one or more pinned M2 releases are missing")
    if any(tuple(row[:3]) != (1, 0, 0) for row in boundaries):
        raise RuntimeError("M2 release boundary is not accepted research-only data")
    manifest_hashes = (str(history_run[0][3]), *(str(row[4]) for row in daily_runs))

    placeholders = ",".join(["%s"] * len(ACCEPTANCE_SYMBOLS))
    references = _query(
        connection,
        "SELECT r.symbol, r.exchange, r.name, r.ipo_date "
        "FROM m2_history_run_shards s JOIN m2_security_references r "
        "ON r.dataset_id=s.shard_dataset_id "
        f"WHERE s.merged_dataset_id=%s AND r.symbol IN ({placeholders}) ORDER BY r.symbol",
        (PINNED_HISTORY_DATASET_ID, *ACCEPTANCE_SYMBOLS),
    )
    instruments = tuple({
        "symbol": str(symbol), "exchange": str(exchange), "name": str(name),
        "ipo_date": value.isoformat(),
    } for symbol, exchange, name, value in references)

    history_rows = _query(
        connection,
        "SELECT b.symbol,b.business_date,b.open_price,b.high,b.low,b.close_price,b.previous_close,"
        "b.volume_shares,b.amount_cny,t.has_primary_bar,t.is_suspended,t.is_st,"
        "t.limit_up,t.limit_down,t.can_buy,t.can_sell,t.at_limit_up,t.at_limit_down,"
        "t.one_price_limit_up,t.one_price_limit_down,b.row_sha256,t.row_sha256 "
        "FROM m2_history_run_shards s JOIN m2_historical_bars b "
        "ON b.dataset_id=s.shard_dataset_id JOIN m2_tradeability_facts t "
        "ON t.dataset_id=b.dataset_id AND t.symbol=b.symbol AND t.business_date=b.business_date "
        f"WHERE s.merged_dataset_id=%s AND b.symbol IN ({placeholders}) AND b.business_date=%s",
        (PINNED_HISTORY_DATASET_ID, *ACCEPTANCE_SYMBOLS, ACCEPTANCE_SESSIONS[0]),
    )
    daily_rows = _query(
        connection,
        "SELECT b.symbol,b.business_date,b.open_price,b.high,b.low,b.close_price,b.previous_close,"
        "b.volume_shares,b.amount_cny,t.has_primary_bar,t.is_suspended,t.is_st,"
        "t.limit_up,t.limit_down,t.can_buy,t.can_sell,t.at_limit_up,t.at_limit_down,"
        "t.one_price_limit_up,t.one_price_limit_down,b.row_sha256,t.row_sha256 "
        "FROM m2_daily_primary_bars b JOIN m2_daily_tradeability_facts t "
        "ON t.dataset_id=b.dataset_id AND t.symbol=b.symbol AND t.business_date=b.business_date "
        f"WHERE b.dataset_id IN (%s,%s) AND b.symbol IN ({placeholders})",
        (*PINNED_DAILY_RELEASE_IDS, *ACCEPTANCE_SYMBOLS),
    )
    daily_actions = _query(
        connection,
        "SELECT symbol,effective_date FROM m2_daily_adjustment_events "
        f"WHERE dataset_id IN (%s,%s) AND symbol IN ({placeholders})",
        (*PINNED_DAILY_RELEASE_IDS, *ACCEPTANCE_SYMBOLS),
    )
    action_keys = {(str(symbol), effective_date) for symbol, effective_date in daily_actions}
    mutable_rows = [list(row) for row in sorted(history_rows + daily_rows, key=lambda row: (row[0], row[1]))]
    previous_by_symbol: dict[str, tuple[date, Any]] = {}
    previous_origins: dict[tuple[str, date], str] = {}
    for row in mutable_rows:
        symbol, session = str(row[0]), row[1]
        if row[6] is None:
            predecessor = previous_by_symbol.get(symbol)
            expected_index = ACCEPTANCE_SESSIONS.index(session) - 1
            if (
                predecessor is None or expected_index < 0
                or predecessor[0] != ACCEPTANCE_SESSIONS[expected_index]
                or (symbol, session) in action_keys
            ):
                raise RuntimeError(f"previous close cannot be derived safely for {symbol} on {session}")
            row[6] = predecessor[1]
            previous_origins[symbol, session] = "prior_admitted_raw_close"
        else:
            previous_origins[symbol, session] = "stored_m2_raw"
        previous_by_symbol[symbol] = (session, row[5])
    all_rows = sorted(mutable_rows, key=lambda row: (row[1], row[0]))
    bars = tuple({
        "symbol": str(row[0]), "business_date": row[1].isoformat(),
        "open": str(row[2]), "high": str(row[3]), "low": str(row[4]), "close": str(row[5]),
        "previous_close": str(row[6]), "volume": int(row[7]), "total_turnover": str(row[8]),
        "previous_close_origin": previous_origins[str(row[0]), row[1]], "adjustment": "none",
        "source_bar_sha256": str(row[20]), "source_tradeability_sha256": str(row[21]),
    } for row in all_rows)
    facts_list: list[dict[str, Any]] = []
    for row in all_rows:
        symbol, session = str(row[0]), row[1]
        limit_up, limit_down = row[12], row[13]
        if limit_up is None or limit_down is None:
            limit_flags = tuple(bool(row[index]) for index in range(16, 20))
            can_buy, can_sell = bool(row[14]), bool(row[15])
            if bool(row[10]) or can_buy != can_sell or any(limit_flags):
                raise RuntimeError(f"price limits cannot be derived safely for {symbol} on {session}")
            limit_up = (Decimal(str(row[3])) + Decimal("0.01")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            limit_down = (Decimal(str(row[4])) - Decimal("0.01")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            limit_origin = (
                "m2_confirmed_non_limit_acceptance_sentinel_v1"
                if can_buy else "blocked_non_actionable_sentinel_v1"
            )
        else:
            limit_origin = "stored_m2_fact"
        facts_list.append({
            "symbol": symbol, "business_date": session.isoformat(),
            "has_primary_bar": bool(row[9]), "is_suspended": bool(row[10]),
            "is_st": None if row[11] is None else bool(row[11]),
            "limit_up": str(limit_up), "limit_down": str(limit_down),
            "price_limit_origin": limit_origin,
            "can_buy": bool(row[14]), "can_sell": bool(row[15]),
            "at_limit_up": bool(row[16]), "at_limit_down": bool(row[17]),
            "one_price_limit_up": bool(row[18]), "one_price_limit_down": bool(row[19]),
        })
    facts = tuple(facts_list)
    return M2BoundedResearchInput(
        history_dataset_id=PINNED_HISTORY_DATASET_ID,
        daily_release_ids=PINNED_DAILY_RELEASE_IDS,
        sessions=ACCEPTANCE_SESSIONS,
        symbols=ACCEPTANCE_SYMBOLS,
        instruments=instruments,
        bars=bars,
        tradeability=facts,
        source_manifest_sha256s=manifest_hashes,
    )


__all__ = [
    "ACCEPTANCE_SESSIONS", "ACCEPTANCE_SYMBOLS", "M2BoundedResearchInput",
    "PINNED_DAILY_RELEASE_IDS", "PINNED_HISTORY_DATASET_ID",
    "extract_pinned_acceptance_input", "load_bounded_input", "write_bounded_input",
]

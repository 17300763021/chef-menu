"""Content-addressed fallback for a bounded historical-market archive.

The archive exists only for symbols whose admitted public endpoints are
reachable from a development network but consistently unavailable from the
GitHub-hosted runner.  It is immutable, dual-source verified, simulation-only,
and bounded to the M2.3 historical acceptance date.  It must never service a
daily incremental request beyond that date.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from scripts.market_data.adjustment_engine import AdjustmentTimeline
from scripts.market_data.contracts import PRICE_QUANTUM, DailyBar, exchange_for_symbol, normalize_symbol
from scripts.market_data.historical_contracts import AdjustmentEvent, SecurityReference
from scripts.market_data.manifest import sha256


ARCHIVE_SCHEMA_VERSION = "m2-historical-archive-evidence-v1"
ARCHIVE_HISTORY_START = date(2018, 1, 1)
ARCHIVE_BUSINESS_END = date(2026, 7, 24)
ARCHIVE_SYMBOLS = frozenset({"000939", "002005", "600485"})
ARCHIVE_PATH = Path(__file__).resolve().parents[1] / "evidence" / "m2_historical_archive_symbols_v1.json"
PRIMARY_SOURCE = "tencent_archive_frozen"
VERIFICATION_SOURCE = "baostock_frozen"
FACTOR_SOURCE = "akshare_sina_factor_multiplicative_frozen"
STATUS_ONLY_SOURCE = "baostock_status_only_frozen"
AMOUNT_ROUNDING_TOLERANCE_CNY = Decimal("51.00")


def _daily_bar_from_canonical(row: dict[str, Any], expected_source: str) -> DailyBar:
    symbol = normalize_symbol(str(row["symbol"]))
    source = str(row.get("source") or "")
    if source != expected_source:
        raise ValueError(f"unexpected archive source for {symbol}: {source}")
    return DailyBar(
        source=source,
        symbol=symbol,
        exchange=str(row["exchange"]),
        business_date=date.fromisoformat(str(row["business_date"])),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        previous_close=None if row.get("previous_close") is None else Decimal(str(row["previous_close"])),
        volume_shares=int(row["volume_shares"]),
        amount_cny=Decimal(str(row["amount_cny"])),
        turnover_percent=None if row.get("turnover_percent") is None else Decimal(str(row["turnover_percent"])),
        trade_status=str(row["trade_status"]),
        is_st=None if row.get("is_st") is None else bool(row["is_st"]),
        adjustment=str(row.get("adjustment") or "none"),
        schema_version=str(row.get("schema_version") or "m2-daily-bar-v1"),
    )


def _adjustment_from_canonical(row: dict[str, Any]) -> AdjustmentEvent:
    source = str(row.get("source") or "")
    if source != FACTOR_SOURCE:
        raise ValueError(f"unexpected archived factor source: {source}")
    return AdjustmentEvent(
        symbol=normalize_symbol(str(row["symbol"])),
        effective_date=date.fromisoformat(str(row["effective_date"])),
        qfq_factor=Decimal(str(row["qfq_factor"])),
        hfq_factor=Decimal(str(row["hfq_factor"])),
        source=source,
    )


def _reference_from_canonical(row: dict[str, Any]) -> SecurityReference:
    symbol = normalize_symbol(str(row["symbol"]))
    source = str(row.get("source") or "")
    if source != VERIFICATION_SOURCE:
        raise ValueError(f"unexpected archived reference source for {symbol}: {source}")
    return SecurityReference(
        symbol=symbol,
        exchange=str(row["exchange"]),
        name=str(row["name"]),
        ipo_date=date.fromisoformat(str(row["ipo_date"])),
        out_date=None if row.get("out_date") is None else date.fromisoformat(str(row["out_date"])),
        source=source,
    )


def _without_hash(value: dict[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def validate_archive_document(document: dict[str, Any]) -> dict[str, Any]:
    """Validate provenance, hashes, source independence, and row-level agreement."""
    if document.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise ValueError("unsupported historical archive schema")
    if document.get("authoritative") is not False or document.get("simulation_orders_allowed") is not False:
        raise ValueError("historical archive must remain non-authoritative and simulation-disabled")
    if date.fromisoformat(str(document.get("history_start"))) != ARCHIVE_HISTORY_START:
        raise ValueError("historical archive start date changed")
    if date.fromisoformat(str(document.get("business_end"))) != ARCHIVE_BUSINESS_END:
        raise ValueError("historical archive business end changed")
    if document.get("dataset_sha256") != sha256(_without_hash(document, "dataset_sha256")):
        raise ValueError("historical archive dataset hash mismatch")
    symbols = document.get("symbols")
    if not isinstance(symbols, dict) or set(symbols) != ARCHIVE_SYMBOLS:
        raise ValueError("historical archive symbol scope changed")

    for symbol in sorted(ARCHIVE_SYMBOLS):
        payload = symbols[symbol]
        if not isinstance(payload, dict):
            raise ValueError(f"invalid historical archive payload for {symbol}")
        if payload.get("content_sha256") != sha256(_without_hash(payload, "content_sha256")):
            raise ValueError(f"historical archive symbol hash mismatch for {symbol}")
        if payload.get("primary_source") != PRIMARY_SOURCE:
            raise ValueError(f"invalid primary source for archived symbol {symbol}")
        if payload.get("verification_source") != VERIFICATION_SOURCE:
            raise ValueError(f"invalid verification source for archived symbol {symbol}")
        if payload.get("factor_source") != FACTOR_SOURCE:
            raise ValueError(f"invalid factor source for archived symbol {symbol}")
        if len({PRIMARY_SOURCE, VERIFICATION_SOURCE, FACTOR_SOURCE}) != 3:
            raise ValueError("archive source roles must be independently attributed")

        primary_rows = [_daily_bar_from_canonical(row, PRIMARY_SOURCE) for row in payload.get("primary_rows", [])]
        verification_rows = [
            _daily_bar_from_canonical(row, VERIFICATION_SOURCE)
            for row in payload.get("verification_rows", [])
        ]
        primary = {row.business_date: row for row in primary_rows}
        verification = {row.business_date: row for row in verification_rows}
        if not primary or len(primary) != len(primary_rows):
            raise ValueError(f"empty or duplicate primary archive rows for {symbol}")
        if set(primary) != set(verification) or len(verification) != len(verification_rows):
            raise ValueError(f"archive verification inventory mismatch for {symbol}")
        if any(row.symbol != symbol or row.exchange != exchange_for_symbol(symbol) for row in [*primary_rows, *verification_rows]):
            raise ValueError(f"archive symbol or exchange mismatch for {symbol}")
        for business_date in sorted(primary):
            first = primary[business_date]
            second = verification[business_date]
            if (first.open, first.high, first.low, first.close) != (second.open, second.high, second.low, second.close):
                raise ValueError(f"archive OHLC mismatch for {symbol}:{business_date}")
            volume_tolerance = max(100, int(second.volume_shares * 0.001))
            if abs(first.volume_shares - second.volume_shares) > volume_tolerance:
                raise ValueError(f"archive volume-unit mismatch for {symbol}:{business_date}")
            if abs(first.amount_cny - second.amount_cny) > AMOUNT_ROUNDING_TOLERANCE_CNY:
                raise ValueError(f"archive amount-unit mismatch for {symbol}:{business_date}")

        status_rows = payload.get("status_rows", [])
        status_dates = [date.fromisoformat(str(row["business_date"])) for row in status_rows]
        if len(status_dates) != len(set(status_dates)) or not set(primary).issubset(status_dates):
            raise ValueError(f"archive status inventory mismatch for {symbol}")
        events = [_adjustment_from_canonical(row) for row in payload.get("adjustment_events", [])]
        if not events or any(event.symbol != symbol for event in events):
            raise ValueError(f"archive adjustment timeline missing for {symbol}")
        timeline = AdjustmentTimeline(events)
        if timeline.events[0].effective_date > min(primary):
            raise ValueError(f"archive adjustment timeline does not cover {symbol}")
        reference = _reference_from_canonical(payload.get("reference", {}))
        if reference.symbol != symbol:
            raise ValueError(f"archive reference mismatch for {symbol}")
    return document


class FrozenArchiveHistorySource:
    """Strict reader for the versioned three-symbol M2.3 archive."""

    def __init__(self, path: Path = ARCHIVE_PATH) -> None:
        self.path = path
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RuntimeError(f"historical archive is missing: {path}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"historical archive cannot be read: {path}: {error}") from error
        self.document = validate_archive_document(document)
        self.dataset_sha256 = str(self.document["dataset_sha256"])

    @staticmethod
    def supports(symbol: str, start: date, end: date) -> bool:
        code = normalize_symbol(symbol)
        return code in ARCHIVE_SYMBOLS and ARCHIVE_HISTORY_START <= start <= end <= ARCHIVE_BUSINESS_END

    def _payload(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        code = normalize_symbol(symbol)
        if not self.supports(code, start, end):
            raise RuntimeError(
                f"historical archive scope rejected for {code}:{start.isoformat()}:{end.isoformat()}"
            )
        return self.document["symbols"][code]

    def fetch_verification(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        payload = self._payload(symbol, start, end)
        rows = [
            _daily_bar_from_canonical(row, VERIFICATION_SOURCE)
            for row in payload["verification_rows"]
        ]
        result = [row for row in rows if start <= row.business_date <= end]
        if not result:
            raise RuntimeError(f"historical archive verification rows unavailable for {normalize_symbol(symbol)}")
        return result

    def fetch_bundle(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[
        dict[date, DailyBar],
        dict[date, tuple[Decimal, Decimal, Decimal, Decimal]],
        dict[date, tuple[Decimal, Decimal, Decimal, Decimal]],
        list[AdjustmentEvent],
        SecurityReference,
        dict[date, dict[str, str]],
        str,
        str,
    ]:
        payload = self._payload(symbol, start, end)
        raw = {
            row.business_date: row
            for row in (
                _daily_bar_from_canonical(value, PRIMARY_SOURCE)
                for value in payload["primary_rows"]
            )
            if start <= row.business_date <= end
        }
        events = [_adjustment_from_canonical(row) for row in payload["adjustment_events"]]
        timeline = AdjustmentTimeline(events)
        qfq: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        hfq: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        for business_date, bar in sorted(raw.items()):
            qfq_factor, hfq_factor = timeline.factors_on(business_date)
            prices = (bar.open, bar.high, bar.low, bar.close)
            qfq[business_date] = tuple(
                (value * qfq_factor).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
                for value in prices
            )  # type: ignore[assignment]
            hfq[business_date] = tuple(
                (value * hfq_factor).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
                for value in prices
            )  # type: ignore[assignment]
            if min(*qfq[business_date], *hfq[business_date]) <= 0:
                raise ValueError(f"nonpositive archived adjusted price for {symbol}:{business_date}")

        status = {
            business_date: {
                "tradestatus": str(row["tradestatus"]),
                "isST": str(row["isST"]),
                "preclose": str(row["preclose"]),
            }
            for row in payload["status_rows"]
            for business_date in [date.fromisoformat(str(row["business_date"]))]
            if start <= business_date <= end
        }
        if not raw and (
            not status
            or any(str(row.get("tradestatus", "")) != "0" for row in status.values())
        ):
            raise RuntimeError(f"historical archive primary rows unavailable for {normalize_symbol(symbol)}")
        reference = _reference_from_canonical(payload["reference"])
        resolved_primary_source = PRIMARY_SOURCE if raw else STATUS_ONLY_SOURCE
        return raw, qfq, hfq, events, reference, status, resolved_primary_source, FACTOR_SOURCE


def frozen_primary_bar(row: DailyBar) -> DailyBar:
    """Return a source-labelled primary row for deterministic evidence building."""
    return replace(row, source=PRIMARY_SOURCE)


def frozen_verification_bar(row: DailyBar) -> DailyBar:
    """Return a source-labelled verification row for deterministic evidence building."""
    return replace(row, source=VERIFICATION_SOURCE)

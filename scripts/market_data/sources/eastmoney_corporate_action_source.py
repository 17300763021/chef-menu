"""Point-in-time Eastmoney corporate-action inventory for one ex-date.

The inventory is a discovery gate only.  A listed symbol still needs
independent factor and structured-action evidence before an adjusted daily bar
can be accepted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

import requests

from scripts.market_data.contracts import normalize_symbol, parse_date
from scripts.market_data.manifest import sha256


EASTMONEY_SHAREBONUS_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE_NAME = "eastmoney_sharebonus_ex_date"


def _optional_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "nan", "nat"}:
        return None
    return parse_date(text[:10]).isoformat()


def _optional_decimal(value: object, field: str) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "nan", "nat"}:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation as error:
        raise RuntimeError(f"Eastmoney corporate-action {field} is invalid: {value!r}") from error
    if not parsed.is_finite() or parsed < 0:
        raise RuntimeError(f"Eastmoney corporate-action {field} is invalid: {value!r}")
    return format(parsed, "f")


@dataclass(frozen=True, slots=True)
class CorporateActionInventory:
    target_session: date
    records: tuple[dict[str, Any], ...]
    source: str = SOURCE_NAME

    def __post_init__(self) -> None:
        symbols = [str(row["symbol"]) for row in self.records]
        if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
            raise ValueError("corporate-action inventory must contain unique canonically sorted symbols")
        if any(row.get("ex_dividend_date") != self.target_session.isoformat() for row in self.records):
            raise ValueError("corporate-action inventory contains an out-of-scope ex-date")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(str(row["symbol"]) for row in self.records)

    @property
    def evidence_sha256(self) -> str:
        return sha256(list(self.records))

    def canonical(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target_session": self.target_session.isoformat(),
            "record_count": len(self.records),
            "records_sha256": self.evidence_sha256,
            "records": list(self.records),
        }


class EastmoneyCorporateActionSource:
    """Fetch every implemented distribution whose ex-date is the target day."""

    name = SOURCE_NAME

    def __init__(
        self,
        *,
        attempts: int = 3,
        timeout_seconds: float = 25.0,
        backoff_seconds: float = 1.0,
        request_get: Callable[..., Any] | None = None,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if timeout_seconds <= 0 or backoff_seconds < 0:
            raise ValueError("corporate-action source timeout/backoff is invalid")
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        self.backoff_seconds = backoff_seconds
        self._request_get = request_get or requests.get

    @staticmethod
    def _canonical_record(raw: Mapping[str, Any], target: date) -> dict[str, Any]:
        symbol = normalize_symbol(raw.get("SECURITY_CODE"))
        ex_date = _optional_date(raw.get("EX_DIVIDEND_DATE"))
        if ex_date != target.isoformat():
            raise RuntimeError(
                f"Eastmoney corporate-action ex-date mismatch for {symbol}: {ex_date!r}"
            )
        progress = str(raw.get("ASSIGN_PROGRESS") or "").strip()
        if not progress:
            raise RuntimeError(f"Eastmoney corporate-action progress is missing for {symbol}")
        return {
            "symbol": symbol,
            "ex_dividend_date": ex_date,
            "equity_record_date": _optional_date(raw.get("EQUITY_RECORD_DATE")),
            "report_date": _optional_date(raw.get("REPORT_DATE")),
            "notice_date": _optional_date(raw.get("NOTICE_DATE")),
            "assign_progress": progress,
            "cash_per_ten_shares": _optional_decimal(raw.get("PRETAX_BONUS_RMB"), "cash dividend"),
            "bonus_ratio": _optional_decimal(raw.get("BONUS_RATIO"), "bonus ratio"),
            "conversion_ratio": _optional_decimal(raw.get("IT_RATIO"), "conversion ratio"),
            "plan_profile": str(raw.get("IMPL_PLAN_PROFILE") or "").strip(),
        }

    def _page(self, target: date, page: int) -> Mapping[str, Any]:
        params = {
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "ALL",
            "filter": f"(EX_DIVIDEND_DATE='{target.isoformat()}')",
            "pageNumber": str(page),
            "pageSize": "500",
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "source": "WEB",
            "client": "WEB",
        }
        response = self._request_get(
            EASTMONEY_SHAREBONUS_URL,
            params=params,
            timeout=self.timeout_seconds,
            headers={"User-Agent": "chef-menu-m2-daily/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or payload.get("success") is not True:
            raise RuntimeError("Eastmoney corporate-action response was not successful")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("Eastmoney corporate-action response has no result object")
        return result

    def fetch(self, target: date) -> CorporateActionInventory:
        failures: list[str] = []
        for attempt in range(1, self.attempts + 1):
            try:
                first = self._page(target, 1)
                pages = int(first.get("pages") or 0)
                total = int(first.get("count") or first.get("total") or 0)
                first_data = first.get("data") or []
                if not isinstance(first_data, list) or pages < 0 or total < 0:
                    raise RuntimeError("Eastmoney corporate-action pagination metadata is invalid")
                if total and pages < 1:
                    raise RuntimeError("Eastmoney corporate-action pagination is incomplete")
                raw_rows = list(first_data)
                for page in range(2, pages + 1):
                    page_result = self._page(target, page)
                    if int(page_result.get("pages") or 0) != pages:
                        raise RuntimeError("Eastmoney corporate-action page count changed during capture")
                    data = page_result.get("data") or []
                    if not isinstance(data, list):
                        raise RuntimeError("Eastmoney corporate-action page data is invalid")
                    raw_rows.extend(data)
                if len(raw_rows) != total:
                    raise RuntimeError(
                        f"Eastmoney corporate-action row count mismatch: expected {total}, got {len(raw_rows)}"
                    )
                by_symbol: dict[str, dict[str, Any]] = {}
                for raw in raw_rows:
                    if not isinstance(raw, Mapping):
                        raise RuntimeError("Eastmoney corporate-action row is not an object")
                    record = self._canonical_record(raw, target)
                    symbol = record["symbol"]
                    if symbol in by_symbol:
                        qualifier = "conflicting " if by_symbol[symbol] != record else "duplicate "
                        raise RuntimeError(f"{qualifier}Eastmoney corporate-action rows for {symbol}")
                    by_symbol[symbol] = record
                return CorporateActionInventory(
                    target_session=target,
                    records=tuple(by_symbol[symbol] for symbol in sorted(by_symbol)),
                )
            except Exception as error:
                failures.append(f"attempt {attempt}: {type(error).__name__}: {error}")
                if attempt == self.attempts:
                    raise RuntimeError(
                        f"Eastmoney corporate-action inventory unavailable after {self.attempts} attempts: "
                        f"{'; '.join(failures)}"
                    ) from error
                if self.backoff_seconds:
                    time.sleep(self.backoff_seconds * attempt)
        raise AssertionError("unreachable corporate-action retry state")


__all__ = ["CorporateActionInventory", "EastmoneyCorporateActionSource", "SOURCE_NAME"]

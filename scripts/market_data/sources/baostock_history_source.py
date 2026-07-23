"""BaoStock status, security-reference, and adjustment-factor adapter."""

from __future__ import annotations

import time
import socket
from datetime import date
from decimal import Decimal
from typing import Any

from scripts.market_data.contracts import PRICE_QUANTUM, DailyBar, baostock_symbol, decimal_value, normalize_baostock_row, normalize_symbol, parse_date
from scripts.market_data.historical_contracts import AdjustmentEvent, SecurityReference


STATUS_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,isST"
AdjustedOHLC = tuple[Decimal, Decimal, Decimal, Decimal]
BLACKLIST_ERROR_CODES = {"10001011"}


class BaostockHistorySource:
    name = "baostock"

    def __init__(self, attempts: int = 3, timeout_seconds: float = 30.0) -> None:
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        self._previous_socket_timeout: float | None = None

    def __enter__(self) -> "BaostockHistorySource":
        try:
            import baostock as bs
        except ImportError as error:
            raise RuntimeError("baostock is not installed") from error
        self._bs = bs
        self._previous_socket_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout_seconds)
        result = None
        for attempt in range(1, self.attempts + 1):
            result = bs.login()
            if result.error_code == "0":
                return self
            if str(result.error_code) in BLACKLIST_ERROR_CODES:
                socket.setdefaulttimeout(self._previous_socket_timeout)
                raise RuntimeError(f"BaoStock login blocked: {result.error_code} {result.error_msg}")
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        socket.setdefaulttimeout(self._previous_socket_timeout)
        raise RuntimeError(f"BaoStock login failed: {result.error_code} {result.error_msg}")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self._bs.logout()
        finally:
            socket.setdefaulttimeout(self._previous_socket_timeout)

    @staticmethod
    def _rows(query: Any, label: str) -> list[dict[str, str]]:
        if str(query.error_code) != "0":
            raise RuntimeError(f"BaoStock {label} failed: {query.error_code} {query.error_msg}")
        rows: list[dict[str, str]] = []
        while query.next():
            rows.append(dict(zip(query.fields, query.get_row_data(), strict=True)))
        return rows

    def fetch_status(self, symbol: str, start: date, end: date) -> dict[date, dict[str, str]]:
        code = normalize_symbol(symbol)
        query = self._bs.query_history_k_data_plus(
            baostock_symbol(code), STATUS_FIELDS, start_date=start.isoformat(), end_date=end.isoformat(),
            frequency="d", adjustflag="3",
        )
        return {parse_date(row["date"]): row for row in self._rows(query, f"status {code}")}

    @staticmethod
    def bars_from_status(symbol: str, rows: dict[date, dict[str, str]]) -> dict[date, DailyBar]:
        code = normalize_symbol(symbol)
        return {
            business_date: normalize_baostock_row(row, code)
            for business_date, row in rows.items()
            if str(row.get("tradestatus", "")).strip() == "1" and str(row.get("open", "")).strip()
        }

    @staticmethod
    def adjusted_prices_from_rows(rows: list[dict[str, str]]) -> dict[date, AdjustedOHLC]:
        output: dict[date, AdjustedOHLC] = {}
        for row in rows:
            if not str(row.get("open", "")).strip():
                continue
            prices = tuple(decimal_value(row.get(field), field, PRICE_QUANTUM) for field in ("open", "high", "low", "close"))
            if any(value is None for value in prices):
                raise ValueError(f"missing adjusted OHLC for {row.get('date')}")
            output[parse_date(row["date"])] = prices  # type: ignore[assignment]
        return output

    def fetch_adjusted_prices(self, symbol: str, start: date, end: date, adjustflag: str) -> dict[date, AdjustedOHLC]:
        if adjustflag not in {"1", "2"}:
            raise ValueError("adjusted BaoStock prices require adjustflag 1 or 2")
        code = normalize_symbol(symbol)
        query = self._bs.query_history_k_data_plus(
            baostock_symbol(code), STATUS_FIELDS, start_date=start.isoformat(), end_date=end.isoformat(),
            frequency="d", adjustflag=adjustflag,
        )
        rows = self._rows(query, f"adjusted prices {code} adjustflag={adjustflag}")
        return self.adjusted_prices_from_rows(rows)

    def fetch_reference(self, symbol: str) -> SecurityReference:
        code = normalize_symbol(symbol)
        rows = self._rows(self._bs.query_stock_basic(code=baostock_symbol(code)), f"reference {code}")
        if len(rows) != 1 or not rows[0].get("ipoDate"):
            raise RuntimeError(f"BaoStock reference unavailable for {code}")
        row = rows[0]
        out_date = parse_date(row["outDate"]) if row.get("outDate") else None
        return SecurityReference.build(code, row.get("code_name", ""), parse_date(row["ipoDate"]), out_date)

    def fetch_adjustments(self, symbol: str, end: date) -> list[AdjustmentEvent]:
        code = normalize_symbol(symbol)
        query = self._bs.query_adjust_factor(code=baostock_symbol(code), start_date="1990-01-01", end_date=end.isoformat())
        rows = self._rows(query, f"adjustments {code}")
        return [
            AdjustmentEvent.build(code, parse_date(row["dividOperateDate"]), row["foreAdjustFactor"], row["backAdjustFactor"])
            for row in rows if row.get("dividOperateDate")
        ]

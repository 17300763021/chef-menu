"""BaoStock status, security-reference, and adjustment-factor adapter."""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from scripts.market_data.contracts import DailyBar, baostock_symbol, normalize_baostock_row, normalize_symbol, parse_date
from scripts.market_data.historical_contracts import AdjustmentEvent, SecurityReference


STATUS_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,isST"


class BaostockHistorySource:
    name = "baostock"

    def __init__(self, attempts: int = 3) -> None:
        self.attempts = attempts

    def __enter__(self) -> "BaostockHistorySource":
        try:
            import baostock as bs
        except ImportError as error:
            raise RuntimeError("baostock is not installed") from error
        self._bs = bs
        result = None
        for attempt in range(1, self.attempts + 1):
            result = bs.login()
            if result.error_code == "0":
                return self
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        raise RuntimeError(f"BaoStock login failed: {result.error_code} {result.error_msg}")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._bs.logout()

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
            for business_date, row in rows.items() if str(row.get("open", "")).strip()
        }

    def fetch_bars(self, symbol: str, start: date, end: date, adjustflag: str) -> dict[date, DailyBar]:
        if adjustflag not in {"1", "2", "3"}:
            raise ValueError("BaoStock adjustflag must be 1, 2, or 3")
        code = normalize_symbol(symbol)
        query = self._bs.query_history_k_data_plus(
            baostock_symbol(code), STATUS_FIELDS, start_date=start.isoformat(), end_date=end.isoformat(),
            frequency="d", adjustflag=adjustflag,
        )
        rows = self._rows(query, f"bars {code} adjustflag={adjustflag}")
        return {
            parse_date(row["date"]): normalize_baostock_row(row, code)
            for row in rows if str(row.get("open", "")).strip()
        }

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

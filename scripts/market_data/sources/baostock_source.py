"""BaoStock unadjusted daily-bar cross-check adapter."""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from scripts.market_data.contracts import DailyBar, baostock_symbol, normalize_baostock_row, normalize_symbol


FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"


class BaostockSource:
    name = "baostock"

    def __init__(self, attempts: int = 3) -> None:
        self.attempts = attempts

    def __enter__(self) -> "BaostockSource":
        try:
            import baostock as bs
        except ImportError as error:
            raise RuntimeError("baostock is not installed") from error
        self._bs = bs
        result = None
        for attempt in range(1, self.attempts + 1):
            result = bs.login()
            if str(result.error_code) == "0":
                break
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        if result is None or str(result.error_code) != "0":
            raise RuntimeError(f"BaoStock login failed after {self.attempts} attempts: {result.error_code} {result.error_msg}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._bs.logout()

    def fetch(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        code = normalize_symbol(symbol)
        query = None
        for attempt in range(1, self.attempts + 1):
            query = self._bs.query_history_k_data_plus(
                baostock_symbol(code),
                FIELDS,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag="3",
            )
            if str(query.error_code) == "0":
                break
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        assert query is not None
        if str(query.error_code) != "0":
            raise RuntimeError(f"BaoStock query failed for {code} after {self.attempts} attempts: {query.error_code} {query.error_msg}")
        rows: list[DailyBar] = []
        while query.next():
            raw = dict(zip(query.fields, query.get_row_data(), strict=True))
            if not str(raw.get("open", "")).strip():
                continue
            rows.append(normalize_baostock_row(raw, code))
        if not rows:
            raise RuntimeError(f"BaoStock returned no rows for {code}")
        return rows

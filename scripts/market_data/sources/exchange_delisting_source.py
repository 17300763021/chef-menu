"""Official Shanghai and Shenzhen delisting inventories for M2.5 exclusions."""

from __future__ import annotations

import time
from typing import Any, Callable

import akshare as ak
import pandas as pd

from scripts.market_data.industry_contracts import IndustryDelistingEvidence


REQUEST_TIMEOUT_SECONDS = 30


def _column(frame: pd.DataFrame, names: tuple[str, ...]) -> str:
    for name in names:
        if name in frame.columns:
            return name
    raise RuntimeError(f"official delisting inventory missing required column: {names}")


def normalize_delisting_frame(
    frame: pd.DataFrame,
    *,
    exchange: str,
) -> tuple[IndustryDelistingEvidence, ...]:
    if frame is None or frame.empty:
        raise RuntimeError(f"{exchange} official delisting inventory returned no rows")
    code_column = _column(frame, ("证券代码", "公司代码"))
    name_column = _column(frame, ("证券简称", "公司简称"))
    # Suspension is not delisting.  Only an explicit termination date may
    # support a fail-closed exclusion decision.
    date_column = _column(frame, ("终止上市日期",))
    source = "szse_official_delisting" if exchange == "SZ" else "sse_official_delisting"
    rows: list[IndustryDelistingEvidence] = []
    for record in frame.to_dict("records"):
        symbol = str(record.get(code_column) or "").strip().split(".")[0].zfill(6)
        raw_date = record.get(date_column)
        if len(symbol) != 6 or not symbol.isdigit() or pd.isna(raw_date) or str(raw_date).strip() in {"", "-"}:
            continue
        rows.append(IndustryDelistingEvidence.build(
            symbol=symbol,
            delisted_on=raw_date,
            exchange=exchange,
            source=source,
            security_name=record.get(name_column),
        ))
    by_symbol: dict[str, IndustryDelistingEvidence] = {}
    for row in rows:
        existing = by_symbol.get(row.symbol)
        if existing is not None and existing.canonical() != row.canonical():
            raise RuntimeError(f"conflicting official delisting rows for {row.symbol}")
        by_symbol[row.symbol] = row
    if not by_symbol:
        raise RuntimeError(f"{exchange} official delisting inventory has no valid A-share rows")
    return tuple(by_symbol[symbol] for symbol in sorted(by_symbol))


class ExchangeDelistingSource:
    def __init__(
        self,
        *,
        attempts: int = 3,
        timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
        sz_loader: Callable[..., pd.DataFrame] | None = None,
        sh_loader: Callable[..., pd.DataFrame] | None = None,
    ) -> None:
        if attempts < 1 or attempts > 3:
            raise ValueError("official delisting attempts must be between 1 and 3")
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        self.sz_loader = sz_loader or ak.stock_info_sz_delist
        self.sh_loader = sh_loader or ak.stock_info_sh_delist
        self._uses_default_sz = sz_loader is None
        self._uses_default_sh = sh_loader is None

    def _call(self, loader: Callable[..., pd.DataFrame], *, bounded: bool, **kwargs: Any) -> pd.DataFrame:
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                if not bounded:
                    return loader(**kwargs)
                requests_module = __import__(loader.__module__, fromlist=["requests"]).requests
                original_get = requests_module.get
                original_post = requests_module.post

                def bounded_get(*args: Any, **request_kwargs: Any):
                    request_kwargs.setdefault("timeout", self.timeout_seconds)
                    response = original_get(*args, **request_kwargs)
                    response.raise_for_status()
                    return response

                def bounded_post(*args: Any, **request_kwargs: Any):
                    request_kwargs.setdefault("timeout", self.timeout_seconds)
                    response = original_post(*args, **request_kwargs)
                    response.raise_for_status()
                    return response

                requests_module.get = bounded_get
                requests_module.post = bounded_post
                try:
                    return loader(**kwargs)
                finally:
                    requests_module.get = original_get
                    requests_module.post = original_post
            except Exception as error:
                last_error = error
                if attempt < self.attempts:
                    time.sleep(min(2 ** (attempt - 1), 2))
        raise RuntimeError(f"official delisting request failed after {self.attempts} attempt(s): {last_error}")

    def fetch(self) -> tuple[IndustryDelistingEvidence, ...]:
        sz = normalize_delisting_frame(
            self._call(
                self.sz_loader,
                bounded=self._uses_default_sz,
                symbol="\u7ec8\u6b62\u4e0a\u5e02\u516c\u53f8",
            ),
            exchange="SZ",
        )
        sh = normalize_delisting_frame(
            self._call(self.sh_loader, bounded=self._uses_default_sh, symbol="\u5168\u90e8"),
            exchange="SH",
        )
        combined = {row.symbol: row for row in (*sz, *sh)}
        if len(combined) != len(sz) + len(sh):
            raise RuntimeError("official exchange delisting inventories contain duplicate symbols")
        return tuple(combined[symbol] for symbol in sorted(combined))


__all__ = ["ExchangeDelistingSource", "normalize_delisting_frame"]

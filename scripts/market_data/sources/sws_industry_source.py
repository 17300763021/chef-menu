"""TLS-verified official Shenwan industry assignment source."""

from __future__ import annotations

import hashlib
import io
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd
import requests

from scripts.market_data.industry_contracts import SwsAssignmentRecord


SWS_ASSIGNMENT_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"


@dataclass(frozen=True, slots=True)
class SwsAssignmentBundle:
    rows: tuple[SwsAssignmentRecord, ...]
    source_url: str
    raw_sha256: str
    raw_bytes: int


def normalize_sws_frame(frame: pd.DataFrame, scope_symbols: Iterable[str]) -> tuple[SwsAssignmentRecord, ...]:
    required = ("股票代码", "计入日期", "行业代码", "更新日期")
    if not all(column in frame.columns for column in required):
        raise RuntimeError(f"unexpected SWS assignment columns: {list(frame.columns)}")
    scope = set(scope_symbols)
    rows: list[SwsAssignmentRecord] = []
    seen: set[tuple[str, object]] = set()
    for item in frame.loc[:, list(required)].to_dict("records"):
        raw_symbol = str(item["股票代码"] or "").strip()
        if raw_symbol not in scope:
            continue
        row = SwsAssignmentRecord.build(
            symbol=raw_symbol,
            source_effective_from=item["计入日期"],
            industry_code=item["行业代码"],
            source_updated_at=item["更新日期"],
        )
        key = (row.symbol, row.source_effective_from)
        if key in seen:
            raise RuntimeError(f"duplicate official SWS assignment: {row.symbol}:{row.source_effective_from}")
        seen.add(key)
        rows.append(row)
    return tuple(sorted(rows, key=lambda value: (value.symbol, value.source_effective_from)))


class SwsIndustrySource:
    def __init__(
        self,
        *,
        timeout_seconds: int = 60,
        attempts: int = 3,
        requester: Callable[..., requests.Response] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.requester = requester or requests.get

    def fetch(self, scope_symbols: Iterable[str]) -> SwsAssignmentBundle:
        last_error: Exception | None = None
        payload: bytes | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.requester(
                    SWS_ASSIGNMENT_URL,
                    headers={"User-Agent": "Mozilla/5.0 M2-industry-research"},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = bytes(response.content)
                if not payload:
                    raise RuntimeError("official SWS assignment file is empty")
                break
            except Exception as error:
                last_error = error
                if attempt < self.attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))
        if payload is None:
            raise RuntimeError(f"official SWS assignment download failed with TLS verification: {last_error}")
        frame = pd.read_excel(io.BytesIO(payload), dtype=str)
        rows = normalize_sws_frame(frame, scope_symbols)
        if not rows:
            raise RuntimeError("official SWS assignment file has no rows for the frozen scope")
        return SwsAssignmentBundle(
            rows=rows,
            source_url=SWS_ASSIGNMENT_URL,
            raw_sha256=hashlib.sha256(payload).hexdigest(),
            raw_bytes=len(payload),
        )

"""Tencent archive fallback for point-in-time A-share daily history.

The public Tencent response includes raw OHLC, volume in lots, turnover, and
amount in ten-thousand CNY.  AKShare's convenience frame intentionally exposes
only the first six fields, so this adapter parses the public response directly
to preserve units and audit provenance.  It is a bounded fallback only; it does
not replace the admitted Eastmoney/Sina primary path.
"""

from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from typing import Any

from scripts.market_data.contracts import (
    AMOUNT_QUANTUM,
    PRICE_QUANTUM,
    TURNOVER_QUANTUM,
    DailyBar,
    decimal_value,
    exchange_for_symbol,
    int_value,
    normalize_symbol,
    parse_date,
)


class TencentHistorySource:
    name = "tencent_archive"
    endpoint = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"

    def __init__(self, timeout_seconds: float = 20.0, attempts: int = 2) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts

    @staticmethod
    def _vendor_symbol(symbol: str) -> str:
        code = normalize_symbol(symbol)
        return ("sh" if exchange_for_symbol(code) == "SSE" else "sz") + code

    def _request_block(self, vendor_symbol: str, start: date, end: date, adjust: str) -> list[list[Any]]:
        try:
            import requests
        except ImportError as error:
            raise RuntimeError("requests is not installed") from error
        if adjust not in {"", "hfq"}:
            raise ValueError("Tencent archive supports raw or hfq history")
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                params = {
                    "_var": f"kline_day{adjust}{start.year}",
                    "param": (
                        f"{vendor_symbol},day,{start.isoformat()},{end.isoformat()},640,{adjust}"
                    ),
                    "r": "0.8205512681390605",
                }
                response = requests.get(self.endpoint, params=params, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload_text = response.text
                separator = payload_text.find("=")
                if separator < 0:
                    raise RuntimeError("Tencent response is missing its JSON assignment")
                payload = json.loads(payload_text[separator + 1 :])
                security = payload.get("data", {}).get(vendor_symbol)
                if not isinstance(security, dict):
                    raise RuntimeError("Tencent response is missing the requested security")
                rows = security.get("hfqday" if adjust == "hfq" else "day")
                if isinstance(rows, list):
                    return rows
                raise RuntimeError("Tencent response contains no daily rows")
            except Exception as error:
                last_error = error
                if attempt < self.attempts:
                    time.sleep(2 ** (attempt - 1))
        assert last_error is not None
        raise RuntimeError(f"Tencent history request failed for {vendor_symbol}: {last_error}") from last_error

    def _rows(self, symbol: str, start: date, end: date, adjust: str) -> list[list[Any]]:
        vendor_symbol = self._vendor_symbol(symbol)
        output: dict[date, list[Any]] = {}
        block_start = start
        while block_start <= end:
            block_end = min(end, date(min(block_start.year + 1, end.year), 12, 31))
            for row in self._request_block(vendor_symbol, block_start, block_end, adjust):
                if not isinstance(row, list) or len(row) < 6:
                    raise RuntimeError(f"Tencent returned a malformed daily row for {vendor_symbol}")
                business_date = parse_date(row[0])
                if start <= business_date <= end:
                    output[business_date] = row
            block_start = date(block_end.year + 1, 1, 1)
        if not output:
            raise RuntimeError(f"Tencent returned no {adjust or 'raw'} rows for {normalize_symbol(symbol)}")
        return [output[key] for key in sorted(output)]

    def fetch_raw(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        code = normalize_symbol(symbol)
        result: list[DailyBar] = []
        for row in self._rows(code, start, end, ""):
            if len(row) < 9:
                raise RuntimeError(f"Tencent raw row lacks amount/turnover fields for {code}:{row[0]}")
            volume_lots = decimal_value(row[5], "Tencent volume(lots)", Decimal("0.01"))
            amount_ten_thousand = decimal_value(row[8], "Tencent amount(10k CNY)", Decimal("0.0001"))
            turnover = decimal_value(row[7], "Tencent turnover(%)", TURNOVER_QUANTUM, allow_blank=True)
            assert volume_lots is not None and amount_ten_thousand is not None
            result.append(DailyBar(
                source=self.name,
                symbol=code,
                exchange=exchange_for_symbol(code),
                business_date=parse_date(row[0]),
                open=decimal_value(row[1], "Tencent open", PRICE_QUANTUM),  # type: ignore[arg-type]
                close=decimal_value(row[2], "Tencent close", PRICE_QUANTUM),  # type: ignore[arg-type]
                high=decimal_value(row[3], "Tencent high", PRICE_QUANTUM),  # type: ignore[arg-type]
                low=decimal_value(row[4], "Tencent low", PRICE_QUANTUM),  # type: ignore[arg-type]
                previous_close=None,
                volume_shares=int_value(volume_lots * Decimal("100"), "Tencent volume(shares)"),
                amount_cny=(amount_ten_thousand * Decimal("10000")).quantize(AMOUNT_QUANTUM),
                turnover_percent=turnover,
                trade_status="trading" if volume_lots > 0 else "unknown_zero_volume",
                is_st=None,
            ))
        return result

"""Sina historical raw prices exposed through pinned AKShare for M2.3 verification."""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

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


class AkshareHistorySource:
    name = "akshare_sina"

    def __init__(self, timeout_seconds: float = 30.0, attempts: int = 3) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts

    def _frame(self, symbol: str, start: date, end: date):
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare is not installed") from error
        frame = None
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                prefix = "sh" if exchange_for_symbol(symbol) == "SSE" else "sz"
                frame = ak.stock_zh_a_daily(
                    symbol=f"{prefix}{symbol}", start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"), adjust="",
                )
                if frame is not None and not frame.empty:
                    return frame
            except Exception as error:
                last_error = error
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        suffix = f": {last_error}" if last_error else ""
        raise RuntimeError(f"Sina returned no raw rows for {symbol}{suffix}")

    def fetch_raw(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        code = normalize_symbol(symbol)
        frame = self._frame(code, start, end)
        rows: list[DailyBar] = []
        for raw in frame.to_dict(orient="records"):
            volume = int_value(raw.get("volume"), "volume(shares)")
            amount = decimal_value(raw.get("amount"), "amount(CNY)", AMOUNT_QUANTUM)
            turnover_ratio = decimal_value(raw.get("turnover"), "turnover(ratio)", TURNOVER_QUANTUM, allow_blank=True)
            assert amount is not None
            rows.append(DailyBar(
                source=self.name, symbol=code, exchange=exchange_for_symbol(code),
                business_date=parse_date(raw.get("date")),
                open=decimal_value(raw.get("open"), "open", PRICE_QUANTUM),  # type: ignore[arg-type]
                high=decimal_value(raw.get("high"), "high", PRICE_QUANTUM),  # type: ignore[arg-type]
                low=decimal_value(raw.get("low"), "low", PRICE_QUANTUM),  # type: ignore[arg-type]
                close=decimal_value(raw.get("close"), "close", PRICE_QUANTUM),  # type: ignore[arg-type]
                previous_close=None, volume_shares=volume, amount_cny=amount,
                turnover_percent=None if turnover_ratio is None else turnover_ratio * Decimal("100"),
                trade_status="trading" if volume > 0 else "unknown_zero_volume", is_st=None,
            ))
        return rows

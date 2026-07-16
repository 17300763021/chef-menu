"""AKShare/Eastmoney unadjusted daily-bar adapter."""

from __future__ import annotations

import time
from datetime import date

from scripts.market_data.contracts import DailyBar, normalize_akshare_row, normalize_symbol


class AkshareSource:
    name = "akshare_eastmoney"

    def __init__(self, timeout_seconds: float = 20.0, attempts: int = 3) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts

    def fetch(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare is not installed") from error
        code = normalize_symbol(symbol)
        frame = None
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                frame = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="",
                    timeout=self.timeout_seconds,
                )
                if frame is not None and not frame.empty:
                    break
            except Exception as error:
                last_error = error
            if attempt < self.attempts:
                time.sleep(2 ** (attempt - 1))
        if frame is None or frame.empty:
            suffix = f": {last_error}" if last_error else ""
            raise RuntimeError(f"AKShare returned no rows for {code} after {self.attempts} attempts{suffix}")
        return [normalize_akshare_row(row, code) for row in frame.to_dict(orient="records")]

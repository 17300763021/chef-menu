"""BaoStock trading-calendar adapter used as the independent M2.2 check."""

from __future__ import annotations

from datetime import date
import time

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import parse_date


class BaostockCalendarSource:
    name = "baostock_calendar"

    def __init__(self, attempts: int = 5, backoff_seconds: float = 2.0) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds

    def fetch(self, start: date, end: date) -> TradingCalendar:
        try:
            import baostock as bs
        except ImportError as error:
            raise RuntimeError("baostock is not installed") from error
        failures: list[str] = []
        for attempt in range(1, self.attempts + 1):
            logged_in = False
            try:
                login = bs.login()
                if login.error_code != "0":
                    raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
                logged_in = True
                result = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
                if result.error_code != "0":
                    raise RuntimeError(f"BaoStock calendar failed: {result.error_code} {result.error_msg}")
                dates: list[date] = []
                while result.next():
                    row = dict(zip(result.fields, result.get_row_data()))
                    if row["is_trading_day"] == "1":
                        dates.append(parse_date(row["calendar_date"]))
                return TradingCalendar.build(self.name, start, end, dates)
            except Exception as error:
                failures.append(f"attempt {attempt}: {type(error).__name__}: {error}")
                if attempt == self.attempts:
                    raise RuntimeError(f"BaoStock calendar unavailable after {self.attempts} attempts: {'; '.join(failures)}") from error
                if self.backoff_seconds:
                    time.sleep(self.backoff_seconds * attempt)
            finally:
                if logged_in:
                    try:
                        bs.logout()
                    except Exception:
                        pass
        raise RuntimeError("BaoStock calendar unavailable")

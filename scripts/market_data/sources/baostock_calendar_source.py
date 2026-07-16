"""BaoStock trading-calendar adapter used as the independent M2.2 check."""

from __future__ import annotations

from datetime import date

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import parse_date


class BaostockCalendarSource:
    name = "baostock_calendar"

    def fetch(self, start: date, end: date) -> TradingCalendar:
        try:
            import baostock as bs
        except ImportError as error:
            raise RuntimeError("baostock is not installed") from error
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
        try:
            result = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
            if result.error_code != "0":
                raise RuntimeError(f"BaoStock calendar failed: {result.error_code} {result.error_msg}")
            dates: list[date] = []
            while result.next():
                row = dict(zip(result.fields, result.get_row_data()))
                if row["is_trading_day"] == "1":
                    dates.append(parse_date(row["calendar_date"]))
            return TradingCalendar.build(self.name, start, end, dates)
        finally:
            bs.logout()

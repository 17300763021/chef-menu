"""AKShare/Sina trading-calendar adapter."""

from __future__ import annotations

from datetime import date

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import parse_date


class AkshareCalendarSource:
    name = "akshare_sina_calendar"

    def fetch(self, start: date, end: date) -> TradingCalendar:
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare is not installed") from error
        frame = ak.tool_trade_date_hist_sina()
        dates = [parse_date(value) for value in frame["trade_date"].tolist()]
        return TradingCalendar.build(self.name, start, end, (value for value in dates if start <= value <= end))

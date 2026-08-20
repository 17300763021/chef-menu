"""RQAlpha public mod that installs the bounded M2 research data source."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from rqalpha.const import INSTRUMENT_TYPE, MARKET, TRADING_CALENDAR_TYPE
from rqalpha.interface import AbstractDataSource, AbstractMod
from rqalpha.model.instrument import Instrument
from rqalpha.utils.datetime_func import convert_date_to_int

from scripts.simulation.m2_history_source import M2BoundedResearchInput, load_bounded_input


__config__ = {"input_path": None, "priority": 1}

BAR_DTYPE = np.dtype([
    ("datetime", "<u8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
    ("close", "<f8"), ("volume", "<f8"), ("total_turnover", "<f8"),
    ("limit_up", "<f8"), ("limit_down", "<f8"),
])


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


class M2BoundedRQAlphaDataSource(AbstractDataSource):
    """Daily stock source; unsupported markets and frequencies fail closed."""

    def __init__(self, value: M2BoundedResearchInput) -> None:
        self.value = value
        self._calendar = pd.DatetimeIndex(value.sessions)
        self._instruments: dict[str, Instrument] = {}
        self._aliases: dict[str, Instrument] = {}
        for row in value.instruments:
            symbol = str(row["symbol"])
            exchange = "XSHG" if str(row["exchange"]) == "SSE" else "XSHE"
            instrument = Instrument({
                "order_book_id": f"{symbol}.{exchange}",
                "symbol": str(row["name"]),
                "round_lot": 100,
                "listed_date": str(row["ipo_date"]),
                "de_listed_date": "2999-12-31",
                "type": "CS",
                "exchange": exchange,
                "board_type": "MainBoard",
                "market_tplus": 1,
            }, market=MARKET.CN)
            self._instruments[instrument.order_book_id] = instrument
            self._aliases[symbol] = self._aliases[instrument.symbol] = instrument
            self._aliases[instrument.order_book_id] = instrument

        facts = {
            (str(row["symbol"]), date.fromisoformat(str(row["business_date"]))): dict(row)
            for row in value.tradeability
        }
        by_symbol: dict[str, list[tuple[Any, ...]]] = {symbol: [] for symbol in value.symbols}
        for row in value.bars:
            symbol = str(row["symbol"])
            session = date.fromisoformat(str(row["business_date"]))
            fact = facts[symbol, session]
            by_symbol[symbol].append((
                np.uint64(convert_date_to_int(session)), float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]), float(row["volume"]),
                float(row["total_turnover"]), float(fact["limit_up"]), float(fact["limit_down"]),
            ))
        self._bars = {
            symbol: np.array(sorted(rows, key=lambda item: item[0]), dtype=BAR_DTYPE)
            for symbol, rows in by_symbol.items()
        }
        self._facts = facts

    def get_instruments(
        self,
        id_or_syms: Iterable[str] | None = None,
        types: Iterable[INSTRUMENT_TYPE] | None = None,
    ) -> Iterable[Instrument]:
        if id_or_syms is not None:
            seen: set[str] = set()
            for key in id_or_syms:
                instrument = self._aliases.get(str(key))
                if instrument is not None and instrument.order_book_id not in seen:
                    seen.add(instrument.order_book_id)
                    yield instrument
            return
        requested = set(types or [INSTRUMENT_TYPE.CS])
        if INSTRUMENT_TYPE.CS in requested:
            yield from self._instruments.values()

    def get_trading_calendars(self):
        return {TRADING_CALENDAR_TYPE.CN_STOCK: self._calendar}

    def _symbol(self, instrument_or_id: Any) -> str:
        order_book_id = getattr(instrument_or_id, "order_book_id", instrument_or_id)
        symbol = str(order_book_id).split(".", 1)[0]
        if symbol not in self.value.symbols:
            raise LookupError(f"instrument is outside the admitted M2 scope: {symbol}")
        return symbol

    def _bar_array(self, instrument: Any) -> np.ndarray:
        return self._bars[self._symbol(instrument)]

    def get_bar(self, instrument: Any, dt: Any, frequency: str):
        if frequency != "1d":
            raise NotImplementedError("M3.3 accepts daily bars only")
        bars = self._bar_array(instrument)
        target = np.uint64(convert_date_to_int(_as_date(dt)))
        index = bars["datetime"].searchsorted(target)
        if index >= len(bars) or bars["datetime"][index] != target:
            return None
        return bars[index]

    def history_bars(
        self, instrument: Any, bar_count: int | None, frequency: str, fields: Any,
        dt: datetime, skip_suspended: bool = True, include_now: bool = False,
        adjust_type: str = "pre", adjust_orig: datetime | None = None,
    ):
        if frequency != "1d":
            raise NotImplementedError("M3.3 accepts daily bars only")
        if adjust_type not in {"none", "pre"}:
            raise ValueError("vendor post-adjusted M2 prices are not admitted")
        bars = self._bar_array(instrument)
        target = np.uint64(convert_date_to_int(_as_date(dt)))
        end = bars["datetime"].searchsorted(target, side="right")
        selected = bars[:end] if bar_count is None else bars[max(0, end - bar_count):end]
        valid = set(BAR_DTYPE.names or ())
        requested = [fields] if isinstance(fields, str) else fields
        if requested is not None and any(field not in valid for field in requested):
            raise ValueError(f"unsupported history field: {fields}")
        return selected if fields is None else selected[fields]

    def is_suspended(self, order_book_id: str, dates: Iterable[Any]) -> list[bool]:
        symbol = self._symbol(order_book_id)
        return [bool(self._facts.get((symbol, _as_date(value)), {}).get("is_suspended", True)) for value in dates]

    def is_st_stock(self, order_book_id: str, dates: Iterable[Any]) -> list[bool]:
        symbol = self._symbol(order_book_id)
        return [self._facts.get((symbol, _as_date(value)), {}).get("is_st") is True for value in dates]

    def get_open_auction_bar(self, instrument: Any, dt: Any):
        bar = self.get_bar(instrument, dt, "1d")
        if bar is None:
            return {name: np.nan for name in ("datetime", "open", "limit_up", "limit_down", "volume", "total_turnover")}
        return {name: bar[name] for name in ("datetime", "open", "limit_up", "limit_down", "volume", "total_turnover")}

    def get_open_auction_volume(self, instrument: Any, dt: Any) -> float:
        bar = self.get_bar(instrument, dt, "1d")
        return 0.0 if bar is None else float(bar["volume"])

    def get_dividend(self, instrument: Any):
        return None

    def get_split(self, instrument: Any):
        return None

    def get_share_transformation(self, order_book_id: str):
        return None

    def available_data_range(self, frequency: str):
        if frequency != "1d":
            raise NotImplementedError
        return self.value.sessions[0], self.value.sessions[-1]

    def get_yield_curve(self, start_date: Any, end_date: Any, tenor: Any = None):
        return pd.DataFrame()

    def get_settle_price(self, instrument: Any, value: date):
        return np.nan

    def history_ticks(self, instrument: Any, count: int, dt: Any):
        raise NotImplementedError("tick data is outside M3.3")

    def current_snapshot(self, instrument: Any, frequency: str, dt: Any):
        raise NotImplementedError("intraday snapshots are outside M3.3")

    def get_trading_minutes_for(self, instrument: Any, trading_dt: Any):
        raise NotImplementedError("minute data is outside M3.3")

    def get_futures_trading_parameters(self, instrument: Any, dt: Any):
        raise NotImplementedError("futures are outside the simulation-only stock scope")

    def get_merge_ticks(self, order_book_id_list: Any, trading_date: Any, last_dt: Any = None):
        raise NotImplementedError("tick data is outside M3.3")

    def get_algo_bar(self, id_or_ins: Any, start_min: int, end_min: int, dt: Any):
        raise NotImplementedError("algorithmic intraday orders are outside M3.3")


class M2DataSourceMod(AbstractMod):
    def start_up(self, env: Any, mod_config: Any) -> None:
        path = getattr(mod_config, "input_path", None)
        if not path:
            raise ValueError("M2 RQAlpha mod requires an explicit bounded input path")
        env.set_data_source(M2BoundedRQAlphaDataSource(load_bounded_input(Path(path))))

    def tear_down(self, code: Any, exception: Exception | None = None) -> None:
        return None


def load_mod() -> M2DataSourceMod:
    return M2DataSourceMod()


__all__ = ["M2BoundedRQAlphaDataSource", "load_mod"]

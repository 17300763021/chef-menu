"""CSI 300/500 benchmark bars with independent Tencent verification."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable

import pandas as pd

from scripts.market_data.contracts import parse_date


INDEX_SCHEMA_VERSION = "m2-index-bars-v1"
INDEX_CODES = ("000300", "000905")
PRICE_QUANTUM = Decimal("0.01")


def _decimal(value: Any, quantum: Decimal = PRICE_QUANTUM) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid index decimal: {value!r}") from error
    if not result.is_finite():
        raise ValueError(f"non-finite index decimal: {value!r}")
    return result


@dataclass(frozen=True, slots=True)
class IndexBar:
    source: str
    index_code: str
    business_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_shares: int | None
    amount_cny: Decimal | None
    schema_version: str = INDEX_SCHEMA_VERSION

    @property
    def key(self) -> tuple[str, date]:
        return self.index_code, self.business_date

    def canonical(self) -> dict[str, Any]:
        row = asdict(self)
        row["business_date"] = self.business_date.isoformat()
        for field in ("open", "high", "low", "close", "amount_cny"):
            value = row[field]
            row[field] = None if value is None else format(value, "f")
        return row


def normalize_official(frame: pd.DataFrame, requested_code: str) -> list[IndexBar]:
    columns = {"\u65e5\u671f", "\u6307\u6570\u4ee3\u7801", "\u5f00\u76d8", "\u6700\u9ad8", "\u6700\u4f4e", "\u6536\u76d8", "\u6210\u4ea4\u91cf", "\u6210\u4ea4\u91d1\u989d"}
    if not columns.issubset(frame.columns):
        raise RuntimeError(f"unexpected CSI index columns: {list(frame.columns)}")
    rows: list[IndexBar] = []
    for raw in frame.to_dict("records"):
        code = str(raw["\u6307\u6570\u4ee3\u7801"]).strip().zfill(6)
        if code != requested_code:
            raise RuntimeError(f"CSI returned index {code} for {requested_code}")
        price_values = [raw["\u5f00\u76d8"], raw["\u6700\u9ad8"], raw["\u6700\u4f4e"], raw["\u6536\u76d8"]]
        if any(str(value).strip().lower() in {"", "none", "nan", "nat"} for value in price_values):
            continue
        volume_raw = raw["\u6210\u4ea4\u91cf"]
        amount_raw = raw["\u6210\u4ea4\u91d1\u989d"]
        rows.append(IndexBar(
            source="csi_official_history",
            index_code=code,
            business_date=parse_date(raw["\u65e5\u671f"]),
            open=_decimal(raw["\u5f00\u76d8"]), high=_decimal(raw["\u6700\u9ad8"]),
            low=_decimal(raw["\u6700\u4f4e"]), close=_decimal(raw["\u6536\u76d8"]),
            volume_shares=None if str(volume_raw).strip().lower() in {"", "none", "nan"} else int(Decimal(str(volume_raw))),
            amount_cny=None if str(amount_raw).strip().lower() in {"", "none", "nan"} else _decimal(Decimal(str(amount_raw)) * Decimal("100000000"), Decimal("0.01")),
        ))
    return sorted(rows, key=lambda row: row.key)


def normalize_tencent(frame: pd.DataFrame, requested_code: str) -> list[IndexBar]:
    columns = {"date", "open", "high", "low", "close", "amount"}
    if not columns.issubset(frame.columns):
        raise RuntimeError(f"unexpected Tencent index columns: {list(frame.columns)}")
    return [IndexBar(
        source="akshare_tencent_index",
        index_code=requested_code,
        business_date=parse_date(raw["date"]),
        open=_decimal(raw["open"]), high=_decimal(raw["high"]), low=_decimal(raw["low"]), close=_decimal(raw["close"]),
        volume_shares=int(Decimal(str(raw["amount"])) * Decimal("100")),
        amount_cny=None,
    ) for raw in frame.to_dict("records")]


class IndexBarSource:
    def __init__(
        self,
        *,
        official_loader: Callable[..., pd.DataFrame] | None = None,
        tencent_loader: Callable[..., pd.DataFrame] | None = None,
    ) -> None:
        if official_loader is None or tencent_loader is None:
            import akshare as ak
            official_loader = official_loader or ak.stock_zh_index_hist_csindex
            tencent_loader = tencent_loader or ak.stock_zh_index_daily_tx
        self.official_loader = official_loader
        self.tencent_loader = tencent_loader

    def fetch(self, index_code: str, start: date, end: date) -> tuple[list[IndexBar], list[IndexBar]]:
        if index_code not in INDEX_CODES:
            raise ValueError(f"unsupported benchmark index: {index_code}")
        official = self.official_loader(symbol=index_code, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        tencent = self.tencent_loader(symbol=f"sh{index_code}", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        primary = [row for row in normalize_official(official, index_code) if start <= row.business_date <= end]
        verification = [row for row in normalize_tencent(tencent, index_code) if start <= row.business_date <= end]
        primary_dates = {row.business_date for row in primary}
        official_dates = {parse_date(value) for value in official["\u65e5\u671f"].tolist()}
        for row in verification:
            if row.business_date in official_dates and row.business_date not in primary_dates:
                primary.append(IndexBar(
                    source="tencent_index_gap_fill",
                    index_code=row.index_code,
                    business_date=row.business_date,
                    open=row.open, high=row.high, low=row.low, close=row.close,
                    volume_shares=row.volume_shares, amount_cny=None,
                ))
        return sorted(primary, key=lambda row: row.key), verification

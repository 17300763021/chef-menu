"""Canonical M2.1 daily-bar contract and strict source normalization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


SCHEMA_VERSION = "m2-daily-bar-v1"
PRICE_QUANTUM = Decimal("0.0001")
AMOUNT_QUANTUM = Decimal("0.01")
TURNOVER_QUANTUM = Decimal("0.000001")


def normalize_symbol(value: str) -> str:
    raw = str(value).strip().lower().replace(".", "")
    if raw.startswith(("sh", "sz", "bj")):
        raw = raw[2:]
    if not (len(raw) == 6 and raw.isdigit()):
        raise ValueError(f"invalid A-share symbol: {value!r}")
    return raw


def exchange_for_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    if code.startswith(("5", "6", "9")):
        return "SSE"
    if code.startswith(("0", "1", "2", "3")):
        return "SZSE"
    if code.startswith(("4", "8")):
        return "BSE"
    raise ValueError(f"unsupported A-share symbol: {symbol}")


def baostock_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    exchange = exchange_for_symbol(code)
    if exchange == "BSE":
        raise ValueError("BaoStock admission adapter does not support BSE symbols")
    return f"{'sh' if exchange == 'SSE' else 'sz'}.{code}"


def parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"invalid business date: {value!r}")


def decimal_value(value: Any, field: str, quantum: Decimal, *, allow_blank: bool = False) -> Decimal | None:
    if value is None or str(value).strip().lower() in {"", "nan", "none", "null", "--"}:
        if allow_blank:
            return None
        raise ValueError(f"missing {field}")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as error:
        raise ValueError(f"invalid {field}: {value!r}") from error
    if not number.is_finite():
        raise ValueError(f"non-finite {field}: {value!r}")
    return number.quantize(quantum, rounding=ROUND_HALF_UP)


def int_value(value: Any, field: str) -> int:
    number = decimal_value(value, field, Decimal("1"))
    assert number is not None
    result = int(number)
    if result < 0:
        raise ValueError(f"negative {field}: {value!r}")
    return result


@dataclass(frozen=True, slots=True)
class DailyBar:
    source: str
    symbol: str
    exchange: str
    business_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    previous_close: Decimal | None
    volume_shares: int
    amount_cny: Decimal
    turnover_percent: Decimal | None
    trade_status: str
    is_st: bool | None
    adjustment: str = "none"
    schema_version: str = SCHEMA_VERSION

    @property
    def key(self) -> tuple[str, date]:
        return self.symbol, self.business_date

    def canonical(self) -> dict[str, Any]:
        row = asdict(self)
        row["business_date"] = self.business_date.isoformat()
        for field in ("open", "high", "low", "close", "previous_close", "amount_cny", "turnover_percent"):
            value = row[field]
            row[field] = None if value is None else format(value, "f")
        return row


def normalize_akshare_row(row: dict[str, Any], requested_symbol: str) -> DailyBar:
    symbol = normalize_symbol(row.get("股票代码") or requested_symbol)
    volume_lots = int_value(row.get("成交量"), "成交量(手)")
    amount = decimal_value(row.get("成交额"), "成交额(元)", AMOUNT_QUANTUM)
    assert amount is not None
    turnover = decimal_value(row.get("换手率"), "换手率(%)", TURNOVER_QUANTUM, allow_blank=True)
    return DailyBar(
        source="akshare_eastmoney",
        symbol=symbol,
        exchange=exchange_for_symbol(symbol),
        business_date=parse_date(row.get("日期")),
        open=decimal_value(row.get("开盘"), "开盘", PRICE_QUANTUM),  # type: ignore[arg-type]
        high=decimal_value(row.get("最高"), "最高", PRICE_QUANTUM),  # type: ignore[arg-type]
        low=decimal_value(row.get("最低"), "最低", PRICE_QUANTUM),  # type: ignore[arg-type]
        close=decimal_value(row.get("收盘"), "收盘", PRICE_QUANTUM),  # type: ignore[arg-type]
        previous_close=None,
        volume_shares=volume_lots * 100,
        amount_cny=amount,
        turnover_percent=turnover,
        trade_status="trading" if volume_lots > 0 else "unknown_zero_volume",
        is_st=None,
    )


def normalize_baostock_row(row: dict[str, Any], requested_symbol: str) -> DailyBar:
    symbol = normalize_symbol(row.get("code") or requested_symbol)
    volume = int_value(row.get("volume"), "volume(shares)")
    amount = decimal_value(row.get("amount"), "amount(CNY)", AMOUNT_QUANTUM)
    assert amount is not None
    trade_status = str(row.get("tradestatus", "")).strip()
    st_value = str(row.get("isST", "")).strip()
    return DailyBar(
        source="baostock",
        symbol=symbol,
        exchange=exchange_for_symbol(symbol),
        business_date=parse_date(row.get("date")),
        open=decimal_value(row.get("open"), "open", PRICE_QUANTUM),  # type: ignore[arg-type]
        high=decimal_value(row.get("high"), "high", PRICE_QUANTUM),  # type: ignore[arg-type]
        low=decimal_value(row.get("low"), "low", PRICE_QUANTUM),  # type: ignore[arg-type]
        close=decimal_value(row.get("close"), "close", PRICE_QUANTUM),  # type: ignore[arg-type]
        previous_close=decimal_value(row.get("preclose"), "preclose", PRICE_QUANTUM, allow_blank=True),
        volume_shares=volume,
        amount_cny=amount,
        turnover_percent=decimal_value(row.get("turn"), "turn(%)", TURNOVER_QUANTUM, allow_blank=True),
        trade_status="trading" if trade_status == "1" else "suspended",
        is_st=None if st_value == "" else st_value == "1",
    )


def canonical_rows(rows: Iterable[DailyBar]) -> list[dict[str, Any]]:
    return [row.canonical() for row in sorted(rows, key=lambda item: (item.source, item.symbol, item.business_date))]

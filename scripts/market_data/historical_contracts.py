"""Canonical M2.3 historical price and adjustment contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from scripts.market_data.contracts import PRICE_QUANTUM, decimal_value, exchange_for_symbol, normalize_symbol


HISTORICAL_SCHEMA_VERSION = "m2-historical-market-v1"
FACTOR_QUANTUM = Decimal("0.000001")


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


@dataclass(frozen=True, slots=True)
class SecurityReference:
    symbol: str
    exchange: str
    name: str
    ipo_date: date
    out_date: date | None
    source: str = "baostock"

    @classmethod
    def build(cls, symbol: str, name: str, ipo_date: date, out_date: date | None = None) -> "SecurityReference":
        code = normalize_symbol(symbol)
        return cls(code, exchange_for_symbol(code), str(name).strip(), ipo_date, out_date)

    def canonical(self) -> dict[str, Any]:
        row = asdict(self)
        row["ipo_date"] = self.ipo_date.isoformat()
        row["out_date"] = None if self.out_date is None else self.out_date.isoformat()
        return row


@dataclass(frozen=True, slots=True)
class AdjustmentEvent:
    symbol: str
    effective_date: date
    qfq_factor: Decimal
    hfq_factor: Decimal
    source: str = "baostock"

    @classmethod
    def build(cls, symbol: str, effective_date: date, qfq_factor: Any, hfq_factor: Any) -> "AdjustmentEvent":
        qfq = decimal_value(qfq_factor, "qfq_factor", FACTOR_QUANTUM)
        hfq = decimal_value(hfq_factor, "hfq_factor", FACTOR_QUANTUM)
        assert qfq is not None and hfq is not None
        if qfq <= 0 or hfq <= 0:
            raise ValueError("adjustment factors must be positive")
        return cls(normalize_symbol(symbol), effective_date, qfq, hfq)

    def canonical(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "effective_date": self.effective_date.isoformat(),
            "qfq_factor": _decimal_text(self.qfq_factor),
            "hfq_factor": _decimal_text(self.hfq_factor),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    symbol: str
    exchange: str
    business_date: date
    index_code: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    previous_close: Decimal | None
    volume_shares: int
    amount_cny: Decimal
    turnover_percent: Decimal | None
    qfq_factor: Decimal
    hfq_factor: Decimal
    qfq_open: Decimal
    qfq_high: Decimal
    qfq_low: Decimal
    qfq_close: Decimal
    hfq_open: Decimal
    hfq_high: Decimal
    hfq_low: Decimal
    hfq_close: Decimal
    primary_source: str = "akshare_eastmoney"
    factor_source: str = "baostock"
    schema_version: str = HISTORICAL_SCHEMA_VERSION

    @property
    def key(self) -> tuple[str, date]:
        return self.symbol, self.business_date

    @classmethod
    def build(
        cls,
        *,
        symbol: str,
        business_date: date,
        index_code: str,
        open_price: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        previous_close: Decimal | None,
        volume_shares: int,
        amount_cny: Decimal,
        turnover_percent: Decimal | None,
        qfq_factor: Decimal,
        hfq_factor: Decimal,
        qfq_prices: tuple[Decimal, Decimal, Decimal, Decimal] | None = None,
        hfq_prices: tuple[Decimal, Decimal, Decimal, Decimal] | None = None,
        primary_source: str = "akshare_eastmoney",
    ) -> "HistoricalBar":
        code = normalize_symbol(symbol)
        values = [open_price, high, low, close]
        qfq = [value.quantize(PRICE_QUANTUM) for value in qfq_prices] if qfq_prices else [(value * qfq_factor).quantize(PRICE_QUANTUM) for value in values]
        hfq = [value.quantize(PRICE_QUANTUM) for value in hfq_prices] if hfq_prices else [(value * hfq_factor).quantize(PRICE_QUANTUM) for value in values]
        return cls(
            code, exchange_for_symbol(code), business_date, index_code,
            open_price, high, low, close, previous_close, volume_shares, amount_cny, turnover_percent,
            qfq_factor, hfq_factor, *qfq, *hfq, primary_source,
        )

    def canonical(self) -> dict[str, Any]:
        row = asdict(self)
        row["business_date"] = self.business_date.isoformat()
        for field in (
            "open", "high", "low", "close", "previous_close", "amount_cny", "turnover_percent",
            "qfq_factor", "hfq_factor", "qfq_open", "qfq_high", "qfq_low", "qfq_close",
            "hfq_open", "hfq_high", "hfq_low", "hfq_close",
        ):
            row[field] = _decimal_text(row[field])
        return row

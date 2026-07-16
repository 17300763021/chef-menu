"""Fail-closed A-share tradeability facts derived for M2.3 research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from scripts.market_data.contracts import normalize_symbol


TICK = Decimal("0.01")


def limit_rate(symbol: str, business_date: date, is_st: bool, listing_age_sessions: int) -> Decimal | None:
    code = normalize_symbol(symbol)
    if listing_age_sessions < 5:
        return None
    if is_st:
        return Decimal("0.05")
    if code.startswith(("688", "689")):
        return Decimal("0.20")
    if code.startswith(("300", "301", "302")):
        return Decimal("0.20") if business_date >= date(2020, 8, 24) else Decimal("0.10")
    if code.startswith(("4", "8")):
        return Decimal("0.30")
    return Decimal("0.10")


def rounded_limit(previous_close: Decimal, rate: Decimal, direction: int) -> Decimal:
    return (previous_close * (Decimal("1") + rate * direction)).quantize(TICK, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class TradeabilityFact:
    symbol: str
    business_date: date
    index_code: str
    has_primary_bar: bool
    has_secondary_status: bool
    is_suspended: bool
    is_st: bool | None
    listing_age_sessions: int
    limit_rate: Decimal | None
    limit_up: Decimal | None
    limit_down: Decimal | None
    at_limit_up: bool
    at_limit_down: bool
    one_price_limit_up: bool
    one_price_limit_down: bool
    can_buy: bool
    can_sell: bool
    block_reasons: tuple[str, ...]
    schema_version: str = "m2-tradeability-v1"

    def canonical(self) -> dict[str, Any]:
        row = asdict(self)
        row["business_date"] = self.business_date.isoformat()
        row["limit_rate"] = None if self.limit_rate is None else format(self.limit_rate, "f")
        row["limit_up"] = None if self.limit_up is None else format(self.limit_up, "f")
        row["limit_down"] = None if self.limit_down is None else format(self.limit_down, "f")
        row["block_reasons"] = list(self.block_reasons)
        return row

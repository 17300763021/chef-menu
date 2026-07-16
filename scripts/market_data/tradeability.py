"""A-share price-limit and fail-closed tradeability derivation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from scripts.market_data.tradeability_contracts import TradeabilityFact, limit_rate, rounded_limit


def derive_tradeability(
    *,
    symbol: str,
    business_date: date,
    index_code: str,
    listing_age_sessions: int,
    primary: dict[str, object] | None,
    secondary: dict[str, object] | None,
) -> TradeabilityFact:
    has_primary = primary is not None
    has_secondary = secondary is not None
    status = None if secondary is None else str(secondary.get("tradestatus", "")).strip()
    is_suspended = status == "0"
    st_text = None if secondary is None else str(secondary.get("isST", "")).strip()
    is_st = None if st_text not in {"0", "1"} else st_text == "1"
    previous_close = None
    if secondary is not None and str(secondary.get("preclose", "")).strip():
        previous_close = Decimal(str(secondary["preclose"]))
    rate = None if is_st is None else limit_rate(symbol, business_date, is_st, listing_age_sessions)
    up = rounded_limit(previous_close, rate, 1) if previous_close and rate else None
    down = rounded_limit(previous_close, rate, -1) if previous_close and rate else None
    high = None if primary is None else Decimal(str(primary["high"]))
    low = None if primary is None else Decimal(str(primary["low"]))
    close = None if primary is None else Decimal(str(primary["close"]))
    at_up = bool(up is not None and close is not None and close >= up)
    at_down = bool(down is not None and close is not None and close <= down)
    one_up = bool(at_up and high == low == close)
    one_down = bool(at_down and high == low == close)
    reasons: list[str] = []
    if not has_primary:
        reasons.append("missing_primary_bar")
    if not has_secondary:
        reasons.append("missing_secondary_status")
    if is_suspended:
        reasons.append("suspended")
    if is_st is None:
        reasons.append("unknown_st_status")
    if one_up:
        reasons.append("one_price_limit_up")
    if one_down:
        reasons.append("one_price_limit_down")
    base_blocked = any(reason in reasons for reason in ("missing_primary_bar", "missing_secondary_status", "suspended", "unknown_st_status"))
    return TradeabilityFact(
        symbol=symbol, business_date=business_date, index_code=index_code,
        has_primary_bar=has_primary, has_secondary_status=has_secondary, is_suspended=is_suspended,
        is_st=is_st, listing_age_sessions=listing_age_sessions, limit_rate=rate, limit_up=up, limit_down=down,
        at_limit_up=at_up, at_limit_down=at_down, one_price_limit_up=one_up, one_price_limit_down=one_down,
        can_buy=not base_blocked and not one_up, can_sell=not base_blocked and not one_down,
        block_reasons=tuple(reasons),
    )

"""Strict capital-flow contracts: exact dates, nullable values, and fail-closed availability."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from scripts.market_data.contracts import exchange_for_symbol, normalize_symbol, parse_date


FLOW_SCHEMA_VERSION = "m2-verified-flow-v1"


def _optional_decimal(value: Any) -> Decimal | None:
    text = str(value or "").strip()
    if text.lower() in {"", "none", "nan", "--"}:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"invalid capital-flow decimal: {value!r}") from error
    return result if result.is_finite() else None


@dataclass(frozen=True, slots=True)
class VerifiedFlowFact:
    symbol: str
    business_date: date
    main_net_inflow_cny: Decimal | None
    main_net_inflow_ratio: Decimal | None
    super_large_net_inflow_cny: Decimal | None
    large_net_inflow_cny: Decimal | None
    medium_net_inflow_cny: Decimal | None
    small_net_inflow_cny: Decimal | None
    source: str = "eastmoney_exact_date_flow"
    schema_version: str = FLOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if all(getattr(self, field) is None for field in (
            "main_net_inflow_cny", "main_net_inflow_ratio", "super_large_net_inflow_cny",
            "large_net_inflow_cny", "medium_net_inflow_cny", "small_net_inflow_cny",
        )):
            raise ValueError("a verified flow fact cannot contain only missing values")

    @property
    def key(self) -> tuple[str, date]:
        return self.symbol, self.business_date

    def canonical(self) -> dict[str, Any]:
        row = asdict(self)
        row["business_date"] = self.business_date.isoformat()
        for key, value in tuple(row.items()):
            if isinstance(value, Decimal):
                row[key] = format(value, "f")
        return row


def parse_eastmoney_klines(payload: Mapping[str, Any], symbol: str, required_date: date) -> VerifiedFlowFact:
    """Parse one exact Eastmoney row; never substitute the latest available row."""
    code = normalize_symbol(symbol)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise RuntimeError("Eastmoney flow response is missing data")
    selected: list[str] = []
    for raw in data.get("klines") or []:
        text = str(raw)
        if text.startswith(required_date.isoformat() + ","):
            selected.append(text)
    if len(selected) != 1:
        raise RuntimeError(f"exact flow row required for {code}:{required_date}; got {len(selected)}")
    fields = selected[0].split(",")
    if len(fields) < 13:
        raise RuntimeError("Eastmoney flow row has an unexpected schema")
    return VerifiedFlowFact(
        symbol=code,
        business_date=parse_date(fields[0]),
        main_net_inflow_cny=_optional_decimal(fields[1]),
        small_net_inflow_cny=_optional_decimal(fields[3]),
        medium_net_inflow_cny=_optional_decimal(fields[5]),
        large_net_inflow_cny=_optional_decimal(fields[7]),
        super_large_net_inflow_cny=_optional_decimal(fields[9]),
        main_net_inflow_ratio=_optional_decimal(fields[11]),
    )


class ExactDateFlowSource:
    endpoint = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch(self, symbol: str, business_date: date) -> VerifiedFlowFact:
        import requests

        code = normalize_symbol(symbol)
        market = "1" if exchange_for_symbol(code) == "SSE" else "0"
        response = requests.get(
            self.endpoint,
            params={
                "lmt": "0", "klt": "101", "secid": f"{market}.{code}",
                "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return parse_eastmoney_klines(response.json(), code, business_date)

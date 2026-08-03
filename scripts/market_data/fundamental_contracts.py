"""Strict point-in-time contracts for the M2 fundamental research dataset."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from scripts.market_data.contracts import normalize_symbol, parse_date
from scripts.market_data.manifest import sha256


FUNDAMENTAL_SCHEMA_VERSION = "m2-fundamental-pit-v1"
STATEMENT_TYPES = ("balance", "income", "cashflow")

METRIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "balance": (
        "TOTAL_ASSETS",
        "TOTAL_LIABILITIES",
        "TOTAL_EQUITY",
        "TOTAL_PARENT_EQUITY",
        "MONETARYFUNDS",
    ),
    "income": (
        "OPERATE_INCOME",
        "OPERATE_PROFIT",
        "TOTAL_PROFIT",
        "NETPROFIT",
        "PARENT_NETPROFIT",
        "DEDUCT_PARENT_NETPROFIT",
    ),
    "cashflow": (
        "NETCASH_OPERATE",
        "NETCASH_INVEST",
        "NETCASH_FINANCE",
        "CCE_ADD",
        "END_CCE",
    ),
}


def _date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in (None, "", "NaT", "nan"):
        raise ValueError(f"missing {field}")
    return parse_date(str(value).split(" ", 1)[0])


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in {"", "none", "nan", "nat", "--"}:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid financial decimal: {value!r}") from error
    if not result.is_finite():
        return None
    return result


@dataclass(frozen=True, slots=True)
class FundamentalReport:
    symbol: str
    statement_type: str
    report_date: date
    notice_date: date
    update_date: date
    effective_on: date
    report_type: str
    currency: str
    organization_type: str
    source: str
    source_row_sha256: str
    schema_version: str = FUNDAMENTAL_SCHEMA_VERSION

    @classmethod
    def build(
        cls,
        *,
        symbol: Any,
        statement_type: str,
        report_date: Any,
        notice_date: Any,
        update_date: Any,
        report_type: Any,
        currency: Any,
        organization_type: Any,
        source: str,
        source_row: Mapping[str, Any],
    ) -> "FundamentalReport":
        if statement_type not in STATEMENT_TYPES:
            raise ValueError(f"unsupported statement type: {statement_type}")
        report_day = _date(report_date, "report_date")
        notice_day = _date(notice_date, "notice_date")
        update_day = _date(update_date, "update_date")
        effective = max(notice_day, update_day)
        if notice_day < report_day:
            raise ValueError("financial notice date cannot precede report period end")
        return cls(
            symbol=normalize_symbol(str(symbol)),
            statement_type=statement_type,
            report_date=report_day,
            notice_date=notice_day,
            update_date=update_day,
            effective_on=effective,
            report_type=str(report_type or "").strip(),
            currency=str(currency or "CNY").strip().upper(),
            organization_type=str(organization_type or "").strip(),
            source=str(source).strip(),
            source_row_sha256=sha256(dict(sorted((str(k), None if v is None else str(v)) for k, v in source_row.items()))),
        )

    @property
    def key(self) -> tuple[str, str, date, date]:
        return self.symbol, self.statement_type, self.report_date, self.effective_on

    @property
    def version_id(self) -> str:
        return sha256(self.canonical())

    def canonical(self) -> dict[str, Any]:
        row = asdict(self)
        for field in ("report_date", "notice_date", "update_date", "effective_on"):
            row[field] = row[field].isoformat()
        return row


@dataclass(frozen=True, slots=True)
class FundamentalFact:
    report_version_id: str
    symbol: str
    statement_type: str
    report_date: date
    effective_on: date
    metric_code: str
    value: Decimal
    unit: str = "CNY"

    @property
    def key(self) -> tuple[str, str]:
        return self.report_version_id, self.metric_code

    def canonical(self) -> dict[str, Any]:
        return {
            "report_version_id": self.report_version_id,
            "symbol": self.symbol,
            "statement_type": self.statement_type,
            "report_date": self.report_date.isoformat(),
            "effective_on": self.effective_on.isoformat(),
            "metric_code": self.metric_code,
            "value": format(self.value, "f"),
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class FundamentalVerification:
    symbol: str
    announcement_date: date
    title: str
    announcement_url: str
    source: str = "cninfo_official_announcement"

    def canonical(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "announcement_date": self.announcement_date.isoformat(),
            "title": self.title,
            "announcement_url": self.announcement_url,
            "source": self.source,
        }

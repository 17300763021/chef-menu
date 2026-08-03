"""Versioned point-in-time industry contracts for the M2.5 research dataset."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Any

from scripts.market_data.contracts import normalize_symbol, parse_date


INDUSTRY_SCHEMA_VERSION = "m2-industry-pit-v2"
SW_2021_EFFECTIVE_DATE = date(2021, 7, 30)


def parse_industry_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return parse_date(text)


def normalize_industry_code(value: Any) -> str:
    text = str(value or "").strip().upper().replace(".SI", "")
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.startswith("S"):
        text = text[1:]
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) not in {2, 4, 6}:
        raise ValueError(f"invalid Shenwan industry code: {value!r}")
    return digits


def normalize_assignment_industry_code(value: Any) -> str:
    code = normalize_industry_code(value)
    if len(code) != 6:
        raise ValueError(f"security industry assignment must use a 6-digit code: {value!r}")
    return code


def classification_version(effective_from: date) -> str:
    return "sw_2021" if effective_from >= SW_2021_EFFECTIVE_DATE else "sw_pre_2021"


@dataclass(frozen=True, slots=True)
class IndustryScopeSecurity:
    symbol: str
    ipo_date: date
    out_date: date | None = None

    @classmethod
    def build(cls, symbol: str, ipo_date: Any, out_date: Any = None) -> "IndustryScopeSecurity":
        return cls(
            symbol=normalize_symbol(symbol),
            ipo_date=parse_date(ipo_date),
            out_date=None if out_date in (None, "", "None") else parse_date(out_date),
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ipo_date": self.ipo_date.isoformat(),
            "out_date": None if self.out_date is None else self.out_date.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SwsAssignmentRecord:
    symbol: str
    source_effective_from: date
    industry_code: str
    source_updated_at: datetime | None
    source: str = "sws_official_workbook"
    standard_name: str | None = None
    standard_code: str | None = None

    @classmethod
    def build(
        cls,
        *,
        symbol: Any,
        source_effective_from: Any,
        industry_code: Any,
        source_updated_at: Any,
        source: str = "sws_official_workbook",
        standard_name: Any = None,
        standard_code: Any = None,
    ) -> "SwsAssignmentRecord":
        updated = None
        if source_updated_at not in (None, "", "NaT", "nan"):
            if isinstance(source_updated_at, datetime):
                updated = source_updated_at.replace(tzinfo=None)
            else:
                updated = datetime.fromisoformat(str(source_updated_at).strip().replace("/", "-"))
        return cls(
            symbol=normalize_symbol(str(symbol)),
            source_effective_from=parse_industry_date(source_effective_from),
            industry_code=normalize_assignment_industry_code(industry_code),
            source_updated_at=updated,
            source=str(source).strip(),
            standard_name=None if standard_name in (None, "", "nan") else str(standard_name).strip(),
            standard_code=None if standard_code in (None, "", "nan") else str(standard_code).strip(),
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "source_effective_from": self.source_effective_from.isoformat(),
            "industry_code": self.industry_code,
            "source_updated_at": None if self.source_updated_at is None else self.source_updated_at.isoformat(),
            "source": self.source,
            "standard_name": self.standard_name,
            "standard_code": self.standard_code,
        }


@dataclass(frozen=True, slots=True)
class IndustryInterval:
    symbol: str
    source_effective_from: date
    valid_from: date
    valid_to: date
    industry_code: str
    level1_code: str
    level2_code: str
    level3_code: str
    level1_name: str | None
    level2_name: str | None
    level3_name: str | None
    classification_version: str
    knowledge_status: str
    known_from: date
    source_updated_at: datetime | None
    primary_source: str = "sws_official_workbook"
    schema_version: str = INDUSTRY_SCHEMA_VERSION

    @classmethod
    def build(
        cls,
        *,
        symbol: str,
        source_effective_from: date,
        valid_from: date,
        valid_to: date,
        industry_code: str,
        observed_on: date,
        source_updated_at: datetime | None,
        primary_source: str = "sws_official_workbook",
    ) -> "IndustryInterval":
        code = normalize_assignment_industry_code(industry_code)
        if valid_to <= valid_from:
            raise ValueError("industry interval must have positive duration")
        return cls(
            symbol=normalize_symbol(symbol),
            source_effective_from=source_effective_from,
            valid_from=valid_from,
            valid_to=valid_to,
            industry_code=code,
            level1_code=code[:2],
            level2_code=code[:4],
            level3_code=code,
            level1_name=None,
            level2_name=None,
            level3_name=None,
            classification_version=classification_version(source_effective_from),
            knowledge_status=(
                "point_in_time_observed" if source_effective_from >= observed_on else "historical_reconstructed"
            ),
            known_from=observed_on,
            source_updated_at=source_updated_at,
            primary_source=primary_source,
        )

    def with_names(self, level1: str | None, level2: str | None, level3: str | None) -> "IndustryInterval":
        return replace(
            self,
            level1_name=level1.strip() if level1 and level1.strip() else None,
            level2_name=level2.strip() if level2 and level2.strip() else None,
            level3_name=level3.strip() if level3 and level3.strip() else None,
        )

    @property
    def key(self) -> tuple[str, date]:
        return self.symbol, self.valid_from

    def canonical(self) -> dict[str, Any]:
        row = asdict(self)
        for field in ("source_effective_from", "valid_from", "valid_to", "known_from"):
            row[field] = row[field].isoformat()
        row["source_updated_at"] = None if self.source_updated_at is None else self.source_updated_at.isoformat()
        return row


@dataclass(frozen=True, slots=True)
class IndustryNode:
    node_code: str
    node_name: str
    parent_code: str | None
    level: int
    standard_name: str
    standard_code: str
    termination_date: date | None
    source: str = "cninfo_catalog"

    def canonical(self) -> dict[str, Any]:
        return {
            "node_code": self.node_code,
            "node_name": self.node_name,
            "parent_code": self.parent_code,
            "level": self.level,
            "standard_name": self.standard_name,
            "standard_code": self.standard_code,
            "termination_date": None if self.termination_date is None else self.termination_date.isoformat(),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class IndustryVerification:
    symbol: str
    change_date: date
    industry_code: str
    level1_name: str | None
    level2_name: str | None
    level3_name: str | None
    standard_name: str
    standard_code: str
    source: str = "cninfo_official_api"

    @property
    def key(self) -> tuple[str, date, str, str]:
        return self.symbol, self.change_date, self.industry_code, self.standard_code

    def canonical(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "change_date": self.change_date.isoformat(),
            "industry_code": self.industry_code,
            "level1_name": self.level1_name,
            "level2_name": self.level2_name,
            "level3_name": self.level3_name,
            "standard_name": self.standard_name,
            "standard_code": self.standard_code,
            "source": self.source,
        }

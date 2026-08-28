"""Immutable M2-to-M4 research admission contracts.

The strategy layer accepts explicitly named, hash-verified research releases.
It never selects a latest row, writes the warehouse, or authorizes simulation.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

import pandas as pd

from scripts.strategy.baseline_contracts import content_sha256


RESEARCH_SCHEMA_VERSION = "m4-research-release-v1"
RQALPHA_ADJUSTED_ORIGIN = "rqalpha_public_history_bars_pre_adjusted"
REQUIRED_COMPONENTS = ("history", "daily", "industry", "fundamental", "index", "flow")
REQUIRED_FRAME_COLUMNS = frozenset({
    "adjusted_high", "adjusted_low", "adjusted_close", "raw_close", "amount_cny", "turnover_percent",
    "industry_level1", "total_assets", "total_liabilities", "average_parent_equity",
    "parent_netprofit_ttm", "netcash_operate_ttm", "earnings_variability_8q",
    "main_net_inflow_cny", "listing_age_sessions", "is_st", "delisting_risk",
    "is_suspended", "one_price_limit_up", "one_price_limit_down", "at_limit_down",
    "can_buy", "can_sell", "adjusted_price_origin", "source_row_sha256",
})
FORBIDDEN_VENDOR_ADJUSTED_COLUMNS = frozenset({
    "qfq_open", "qfq_high", "qfq_low", "qfq_close", "qfq_factor",
    "hfq_open", "hfq_high", "hfq_low", "hfq_close", "hfq_factor",
})
_HASH = re.compile(r"^[0-9a-f]{64}$")


class ComponentState(str, Enum):
    ACCEPTED = "accepted"
    DISABLED_OPTIONAL = "disabled_optional"
    MISSING = "missing"
    STALE = "stale"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class ResearchComponent:
    name: str
    dataset_id: str
    manifest_sha256: str
    through_date: date
    state: ComponentState
    expected_count: int
    available_count: int

    def __post_init__(self) -> None:
        if self.name not in REQUIRED_COMPONENTS:
            raise ValueError(f"unknown M4 research component: {self.name}")
        if not self.dataset_id.strip() or "latest" in self.dataset_id.lower():
            raise ValueError("research component requires an explicit non-latest dataset id")
        if not _HASH.fullmatch(self.manifest_sha256):
            raise ValueError("research component requires a lowercase SHA-256 manifest hash")
        if not isinstance(self.through_date, date) or not isinstance(self.state, ComponentState):
            raise ValueError("research component requires typed date and state fields")
        if self.expected_count <= 0 or not 0 <= self.available_count <= self.expected_count:
            raise ValueError("research component counts are invalid")

    @property
    def coverage(self) -> Decimal:
        return Decimal(self.available_count) / Decimal(self.expected_count)


@dataclass(frozen=True, slots=True)
class M4ResearchRelease:
    release_id: str
    business_date: date
    strategy_version: str
    components: tuple[ResearchComponent, ...]
    adjusted_price_origin: str = RQALPHA_ADJUSTED_ORIGIN
    authoritative: bool = False
    simulation_orders_allowed: bool = False
    schema_version: str = RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_SCHEMA_VERSION:
            raise ValueError("unsupported M4 research release schema")
        if self.authoritative or self.simulation_orders_allowed:
            raise ValueError("M4 research release must remain non-authoritative and simulation-only")
        if not self.release_id.strip() or "latest" in self.release_id.lower():
            raise ValueError("M4 release requires a stable explicit id")
        if not isinstance(self.business_date, date) or not self.strategy_version.strip():
            raise ValueError("M4 release requires typed business date and strategy version")
        if self.adjusted_price_origin != RQALPHA_ADJUSTED_ORIGIN:
            raise ValueError("M4 price factors require the RQAlpha public adjusted view")
        names = tuple(component.name for component in self.components)
        if names != REQUIRED_COMPONENTS:
            raise ValueError("M4 components must use the canonical deterministic order")
        for component in self.components:
            if component.through_date < self.business_date and component.name not in {"history", "fundamental"}:
                raise ValueError(f"M4 component is stale for the business date: {component.name}")
        fundamental = self.component("fundamental")
        if fundamental.state is ComponentState.ACCEPTED and fundamental.coverage < Decimal("0.98"):
            raise ValueError("accepted point-in-time fundamentals require at least 98% coverage")
        flow = self.component("flow")
        if flow.state is ComponentState.ACCEPTED and flow.coverage < Decimal("0.98"):
            raise ValueError("accepted verified flow requires at least 98% coverage")
        if flow.state is ComponentState.DISABLED_OPTIONAL and flow.available_count != 0:
            raise ValueError("release-wide disabled flow cannot carry comparable observations")

    def component(self, name: str) -> ResearchComponent:
        return next(component for component in self.components if component.name == name)

    @property
    def actionable_research_ready(self) -> bool:
        required = {"history", "daily", "industry", "fundamental", "index"}
        return all(self.component(name).state is ComponentState.ACCEPTED for name in required)

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self.to_mapping(include_hash=False))

    def to_mapping(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "business_date": self.business_date.isoformat(),
            "strategy_version": self.strategy_version,
            "adjusted_price_origin": self.adjusted_price_origin,
            "authoritative": self.authoritative,
            "simulation_orders_allowed": self.simulation_orders_allowed,
            "actionable_research_ready": self.actionable_research_ready,
            "components": [
                {
                    **asdict(component),
                    "through_date": component.through_date.isoformat(),
                    "state": component.state.value,
                    "coverage": format(component.coverage, "f"),
                }
                for component in self.components
            ],
        }
        if include_hash:
            result["manifest_sha256"] = self.manifest_sha256
        return result


def build_release(
    *, business_date: date, strategy_version: str, components: Sequence[ResearchComponent], release_id: str
) -> M4ResearchRelease:
    return M4ResearchRelease(
        release_id=release_id,
        business_date=business_date,
        strategy_version=strategy_version,
        components=tuple(components),
    )


def validate_research_frame(frame: pd.DataFrame, release: M4ResearchRelease) -> None:
    if not isinstance(frame.index, pd.MultiIndex) or tuple(frame.index.names) != ("datetime", "instrument"):
        raise ValueError("Qlib research frame requires datetime/instrument MultiIndex")
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError("Qlib research frame index must be unique and deterministically sorted")
    missing = sorted(REQUIRED_FRAME_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"M4 research frame missing required columns: {missing}")
    forbidden = sorted(FORBIDDEN_VENDOR_ADJUSTED_COLUMNS & set(frame.columns))
    if forbidden:
        raise ValueError(f"vendor adjusted fields cannot enter M4: {forbidden}")
    datetimes = pd.DatetimeIndex(frame.index.get_level_values("datetime"))
    if datetimes.tz is not None or datetimes.max().date() > release.business_date:
        raise ValueError("M4 research frame contains timezone ambiguity or future data")
    instruments = frame.index.get_level_values("instrument").astype(str)
    if any(not re.fullmatch(r"\d{6}", value) for value in instruments):
        raise ValueError("M4 research frame contains a non-A-share instrument")
    if set(frame["adjusted_price_origin"].dropna().astype(str)) != {RQALPHA_ADJUSTED_ORIGIN}:
        raise ValueError("M4 research frame adjusted prices do not come from RQAlpha")
    hashes = frame["source_row_sha256"].dropna().astype(str)
    if len(hashes) != len(frame) or any(not _HASH.fullmatch(value) for value in hashes):
        raise ValueError("every M4 research row requires immutable source lineage")
    if frame[["adjusted_high", "adjusted_low", "adjusted_close", "raw_close", "amount_cny"]].isna().any().any():
        raise ValueError("M4 price and amount inputs cannot be missing")
    if (frame[["adjusted_high", "adjusted_low", "adjusted_close", "raw_close", "amount_cny"]] <= 0).any().any():
        raise ValueError("M4 price and amount inputs must be positive")
    if ((frame["adjusted_low"] > frame["adjusted_close"]) | (frame["adjusted_close"] > frame["adjusted_high"])).any():
        raise ValueError("M4 adjusted OHLC relationship is invalid")
    if release.component("flow").state is ComponentState.DISABLED_OPTIONAL:
        if frame["main_net_inflow_cny"].notna().any():
            raise ValueError("disabled release-wide flow cannot leak candidate observations")
    else:
        flow_window_missing = frame.groupby(level="instrument", sort=True)["main_net_inflow_cny"].tail(5).isna()
        if flow_window_missing.any():
            raise ValueError("available verified-flow release cannot omit a candidate five-session window")


def release_from_mapping(raw: Mapping[str, Any]) -> M4ResearchRelease:
    components = tuple(
        ResearchComponent(
            name=str(row["name"]), dataset_id=str(row["dataset_id"]),
            manifest_sha256=str(row["manifest_sha256"]),
            through_date=date.fromisoformat(str(row["through_date"])),
            state=ComponentState(str(row["state"])), expected_count=int(row["expected_count"]),
            available_count=int(row["available_count"]),
        )
        for row in raw.get("components", [])
    )
    authoritative = raw.get("authoritative")
    simulation_orders_allowed = raw.get("simulation_orders_allowed")
    if not isinstance(authoritative, bool) or not isinstance(simulation_orders_allowed, bool):
        raise ValueError("M4 authority flags must be JSON booleans")
    result = M4ResearchRelease(
        release_id=str(raw.get("release_id", "")),
        business_date=date.fromisoformat(str(raw.get("business_date", ""))),
        strategy_version=str(raw.get("strategy_version", "")),
        components=components,
        adjusted_price_origin=str(raw.get("adjusted_price_origin", "")),
        authoritative=authoritative,
        simulation_orders_allowed=simulation_orders_allowed,
        schema_version=str(raw.get("schema_version", "")),
    )
    declared = str(raw.get("manifest_sha256", ""))
    if declared and declared != result.manifest_sha256:
        raise ValueError("M4 research manifest hash does not reconcile")
    return result

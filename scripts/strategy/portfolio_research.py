"""M4 portfolio research and recommendation contracts.

This module produces auditable research decisions only. It never calculates
share quantities, submits orders, or mutates an RQAlpha account.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Mapping, Sequence

import pandas as pd

from scripts.strategy.baseline_contracts import FACTOR_NAMES, MarketRegime, content_sha256
from scripts.strategy.market_regime import RegimeAssessment


class ResearchDecision(str, Enum):
    HOLD_REVIEW = "hold_review"
    EXIT_REVIEW = "exit_review"
    NEW_RESEARCH_CANDIDATE = "new_research_candidate"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class OpenPosition:
    symbol: str
    industry_level1: str
    current_weight: Decimal
    planned_risk: Decimal
    original_stop: Decimal
    current_price: Decimal
    entry_price: Decimal
    holding_sessions: int
    stock_return_since_entry: Decimal
    benchmark_return_since_entry: Decimal
    current_rank_percentile: Decimal | None
    prior_week_rank_percentile: Decimal | None


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    industry_level1: str
    factor_score: Decimal
    rank: int
    percentile: Decimal
    current_price: Decimal
    atr20: Decimal
    factor_values: tuple[tuple[str, Decimal | None], ...]

    def __post_init__(self) -> None:
        if tuple(name for name, _ in self.factor_values) != FACTOR_NAMES:
            raise ValueError("candidate factors must use the canonical order")
        if self.current_price <= 0 or self.atr20 <= 0 or self.rank <= 0:
            raise ValueError("candidate price, ATR, and rank must be positive")


@dataclass(frozen=True, slots=True)
class ResearchRecommendation:
    recommendation_id: str
    business_date: date
    symbol: str
    decision: ResearchDecision
    regime: MarketRegime
    strategy_version: str
    research_release_id: str
    reason_codes: tuple[str, ...]
    factor_score: Decimal | None
    factor_values: tuple[tuple[str, Decimal | None], ...]
    planned_weight: Decimal
    planned_risk: Decimal
    entry_lower: Decimal | None
    entry_upper: Decimal | None
    original_stop: Decimal | None
    defensive_price: Decimal | None
    target_prices: tuple[Decimal, ...]
    simulation_order_allowed: bool = False

    def __post_init__(self) -> None:
        if self.simulation_order_allowed:
            raise ValueError("M4 recommendations cannot authorize simulation orders")
        if not self.reason_codes:
            raise ValueError("recommendation requires structured reason codes")
        if self.decision is ResearchDecision.NEW_RESEARCH_CANDIDATE:
            if any(value is None for value in (self.entry_lower, self.entry_upper, self.original_stop, self.defensive_price)):
                raise ValueError("new research candidate is missing structured prices")
            if not self.original_stop < self.defensive_price <= self.entry_lower <= self.entry_upper < self.target_prices[0]:
                raise ValueError("recommendation prices violate the preregistered relationship")


@dataclass(frozen=True, slots=True)
class PortfolioResearchPlan:
    business_date: date
    strategy_version: str
    research_release_id: str
    regime: MarketRegime
    gross_exposure_cap: Decimal
    recommendations: tuple[ResearchRecommendation, ...]
    complete_position_coverage: bool
    full_cash_permitted: bool
    plan_sha256: str


def candidates_from_qlib_scores(features: pd.DataFrame, scores: pd.DataFrame) -> tuple[Candidate, ...]:
    """Join Qlib-owned ranks to transparent factor evidence without rescoring."""
    if not features.index.equals(scores.index):
        raise ValueError("Qlib scores must reconcile one-for-one to the feature index")
    required_score = {"factor_score", "rank", "percentile"}
    required_feature = set(FACTOR_NAMES) | {"industry_level1", "current_price", "atr20"}
    if not required_score.issubset(scores.columns) or not required_feature.issubset(features.columns):
        raise ValueError("candidate conversion is missing Qlib score or factor evidence")
    candidates: list[Candidate] = []
    for key in features.index:
        feature = features.loc[key]
        score = scores.loc[key]
        symbol = str(key[1])
        values = tuple(
            (name, None if pd.isna(feature[name]) else Decimal(str(feature[name])))
            for name in FACTOR_NAMES
        )
        candidates.append(Candidate(
            symbol=symbol,
            industry_level1=str(feature["industry_level1"]),
            factor_score=Decimal(str(score["factor_score"])),
            rank=int(score["rank"]),
            percentile=Decimal(str(score["percentile"])),
            current_price=Decimal(str(feature["current_price"])),
            atr20=Decimal(str(feature["atr20"])),
            factor_values=values,
        ))
    return tuple(sorted(candidates, key=lambda row: (row.rank, row.symbol)))


def _identifier(release_id: str, business_date: date, symbol: str, decision: ResearchDecision) -> str:
    raw = f"{release_id}|{business_date.isoformat()}|{symbol}|{decision.value}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def _holding_decision(
    position: OpenPosition,
    regime: MarketRegime,
    *,
    gross_cap_breached: bool,
    open_risk_breached: bool,
    industry_cap_breached: bool,
) -> tuple[ResearchDecision, tuple[str, ...]]:
    reasons: list[str] = []
    if position.current_price <= position.original_stop:
        reasons.append("ORIGINAL_STOP_REACHED")
    if (
        position.current_rank_percentile is not None
        and position.prior_week_rank_percentile is not None
        and position.current_rank_percentile < Decimal("0.50")
        and position.prior_week_rank_percentile < Decimal("0.50")
    ):
        reasons.append("RANK_BELOW_MEDIAN_TWO_WEEKS")
    if position.holding_sessions >= 20 and position.stock_return_since_entry <= position.benchmark_return_since_entry:
        reasons.append("TIME_STOP_NO_POSITIVE_EXCESS")
    if regime is MarketRegime.CRITICAL_RISK:
        reasons.append("CRITICAL_REGIME_EXIT_REVIEW")
    if gross_cap_breached:
        reasons.append("REGIME_GROSS_CAP_EXCEEDED")
    if open_risk_breached:
        reasons.append("OPEN_RISK_CAP_EXCEEDED")
    if industry_cap_breached:
        reasons.append("INDUSTRY_CAP_EXCEEDED")
    if reasons:
        return ResearchDecision.EXIT_REVIEW, tuple(reasons)
    return ResearchDecision.HOLD_REVIEW, ("OPEN_POSITION_DAILY_RISK_CHECK_PASSED",)


def build_portfolio_research_plan(
    *,
    business_date: date,
    strategy_version: str,
    research_release_id: str,
    regime: RegimeAssessment,
    positions: Sequence[OpenPosition],
    candidates: Sequence[Candidate],
    cooldown_symbols: Sequence[str] = (),
    same_day_exit_symbols: Sequence[str] = (),
) -> PortfolioResearchPlan:
    """Evaluate every open position and allocate bounded research weights."""
    position_symbols = [row.symbol for row in positions]
    if len(position_symbols) != len(set(position_symbols)):
        raise ValueError("authoritative open positions contain duplicate symbols")
    candidate_symbols = [row.symbol for row in candidates]
    if len(candidate_symbols) != len(set(candidate_symbols)):
        raise ValueError("candidate ranking contains duplicate symbols")
    ordered_candidates = sorted(candidates, key=lambda row: (-row.factor_score, row.symbol))
    recommendations: list[ResearchRecommendation] = []
    industry_weight: dict[str, Decimal] = {}
    gross_weight = Decimal("0")
    open_risk = Decimal("0")
    for position in positions:
        industry_weight[position.industry_level1] = industry_weight.get(position.industry_level1, Decimal("0")) + position.current_weight
        gross_weight += position.current_weight
        open_risk += position.planned_risk
    gross_cap_breached = gross_weight > regime.gross_exposure_cap
    open_risk_breached = open_risk > Decimal("0.03")
    for position in sorted(positions, key=lambda row: row.symbol):
        decision, reasons = _holding_decision(
            position, regime.regime,
            gross_cap_breached=gross_cap_breached,
            open_risk_breached=open_risk_breached,
            industry_cap_breached=industry_weight[position.industry_level1] > Decimal("0.20"),
        )
        recommendations.append(ResearchRecommendation(
            recommendation_id=_identifier(research_release_id, business_date, position.symbol, decision),
            business_date=business_date, symbol=position.symbol, decision=decision, regime=regime.regime,
            strategy_version=strategy_version, research_release_id=research_release_id,
            reason_codes=reasons, factor_score=None, factor_values=tuple((name, None) for name in FACTOR_NAMES),
            planned_weight=position.current_weight, planned_risk=position.planned_risk,
            entry_lower=None, entry_upper=None, original_stop=position.original_stop,
            defensive_price=None, target_prices=(),
        ))

    # Ten is the normal target; twelve is a hard ceiling for later transition
    # handling, not permission to fill every acceptance run to the ceiling.
    capacity = max(0, min(10 - len(positions), 12 - len(positions)))
    for candidate in ordered_candidates:
        reasons: list[str] = []
        if candidate.symbol in position_symbols:
            continue
        if candidate.symbol in cooldown_symbols:
            reasons.append("STOP_LOSS_COOLDOWN")
        if candidate.symbol in same_day_exit_symbols:
            reasons.append("SAME_DAY_REENTRY_FORBIDDEN")
        if regime.regime is MarketRegime.CRITICAL_RISK or not regime.data_valid:
            reasons.append("CRITICAL_OR_INVALID_REGIME")
        if capacity <= 0:
            reasons.append("MAXIMUM_HOLDINGS_REACHED")
        stop = candidate.current_price - Decimal("2") * candidate.atr20
        if stop <= 0:
            reasons.append("ATR_STOP_NONPOSITIVE")
        loss_fraction = Decimal("2") * candidate.atr20 / candidate.current_price
        risk_limited_weight = Decimal("0.005") / loss_fraction
        weight = _quantize(min(Decimal("0.08"), risk_limited_weight, max(Decimal("0"), regime.gross_exposure_cap - gross_weight)))
        risk = _quantize(weight * loss_fraction)
        if weight <= 0 or risk <= 0:
            reasons.append("NO_GROSS_OR_RISK_CAPACITY")
        if industry_weight.get(candidate.industry_level1, Decimal("0")) + weight > Decimal("0.20"):
            reasons.append("INDUSTRY_CAP_EXCEEDED")
        if open_risk + risk > Decimal("0.03"):
            reasons.append("OPEN_RISK_CAP_EXCEEDED")
        if reasons:
            decision = ResearchDecision.BLOCKED
            selected_weight = Decimal("0")
            selected_risk = Decimal("0")
            prices = (None, None, None, None, ())
        else:
            decision = ResearchDecision.NEW_RESEARCH_CANDIDATE
            selected_weight, selected_risk = weight, risk
            lower = candidate.current_price - Decimal("0.25") * candidate.atr20
            upper = candidate.current_price + Decimal("0.25") * candidate.atr20
            defensive = candidate.current_price - candidate.atr20
            targets = (candidate.current_price + Decimal("4") * candidate.atr20, candidate.current_price + Decimal("6") * candidate.atr20)
            prices = (lower, upper, stop, defensive, targets)
            reasons = ["QLIB_TRANSPARENT_RANK", "PORTFOLIO_CONSTRAINTS_PASSED"]
            gross_weight += selected_weight
            open_risk += selected_risk
            industry_weight[candidate.industry_level1] = industry_weight.get(candidate.industry_level1, Decimal("0")) + selected_weight
            capacity -= 1
        recommendations.append(ResearchRecommendation(
            recommendation_id=_identifier(research_release_id, business_date, candidate.symbol, decision),
            business_date=business_date, symbol=candidate.symbol, decision=decision, regime=regime.regime,
            strategy_version=strategy_version, research_release_id=research_release_id,
            reason_codes=tuple(reasons), factor_score=candidate.factor_score, factor_values=candidate.factor_values,
            planned_weight=selected_weight, planned_risk=selected_risk,
            entry_lower=prices[0], entry_upper=prices[1], original_stop=prices[2],
            defensive_price=prices[3], target_prices=prices[4],
        ))
    evaluated_positions = {row.symbol for row in recommendations if row.symbol in position_symbols}
    coverage = evaluated_positions == set(position_symbols)
    payload = {
        "business_date": business_date,
        "strategy_version": strategy_version,
        "research_release_id": research_release_id,
        "regime": regime.regime,
        "gross_exposure_cap": regime.gross_exposure_cap,
        "recommendations": recommendations,
        "complete_position_coverage": coverage,
        "full_cash_permitted": True,
    }
    return PortfolioResearchPlan(
        business_date, strategy_version, research_release_id, regime.regime,
        regime.gross_exposure_cap, tuple(recommendations), coverage, True,
        content_sha256(payload),
    )

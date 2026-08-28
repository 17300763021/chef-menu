"""Fail-closed contracts for the preregistered M4 baseline strategy.

This module does not calculate factors, rank securities, create orders, or
maintain an account. Qlib owns scoring/ranking and RQAlpha owns execution and
accounting. M4.1 only freezes and validates the boundary between those layers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


SPEC_PATH = Path(__file__).with_name("specs") / "a_share_adaptive_baseline_v1.json"
V2_SPEC_PATH = Path(__file__).with_name("specs") / "a_share_adaptive_baseline_v2.json"
EXPECTED_SCHEMA_VERSION = "m4-baseline-strategy-contract-v1"
COMPLETE_SCHEMA_VERSION = "m4-baseline-strategy-contract-v2"
FACTOR_NAMES = (
    "residual_momentum",
    "trend_quality",
    "volume_price_liquidity",
    "verified_capital_flow",
    "quality_risk",
)
BASE_WEIGHTS = {
    "residual_momentum": Decimal("0.25"),
    "trend_quality": Decimal("0.15"),
    "volume_price_liquidity": Decimal("0.20"),
    "verified_capital_flow": Decimal("0.15"),
    "quality_risk": Decimal("0.25"),
}
REGIME_CAPS = {
    "strong_bull": Decimal("0.80"),
    "weak_bull": Decimal("0.60"),
    "range": Decimal("0.40"),
    "bear": Decimal("0.20"),
    "critical_risk": Decimal("0.00"),
}
_SYMBOL = re.compile(r"^\d{6}$")
_WEIGHT_QUANTUM = Decimal("0.000001")


class ActionabilityState(str, Enum):
    ACTIONABLE = "actionable"
    BLOCKED = "blocked"


class FactorDataState(str, Enum):
    OBSERVED = "observed"
    DISABLED_RELEASE_WIDE = "disabled_release_wide"
    MISSING_REQUIRED = "missing_required"
    MISSING_CANDIDATE = "missing_candidate"


class MarketRegime(str, Enum):
    STRONG_BULL = "strong_bull"
    WEAK_BULL = "weak_bull"
    RANGE = "range"
    BEAR = "bear"
    CRITICAL_RISK = "critical_risk"


def _decimal(value: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not number.is_finite() or (minimum is not None and number < minimum):
        raise ValueError(f"{field} is outside its permitted range")
    return number


def canonical_json(value: Any) -> bytes:
    if isinstance(value, Decimal):
        value = format(value, "f")
    elif isinstance(value, date):
        value = value.isoformat()
    elif isinstance(value, Enum):
        value = value.value
    elif hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    if isinstance(value, Mapping):
        value = {str(key): json.loads(canonical_json(item)) for key, item in sorted(value.items())}
    elif isinstance(value, (list, tuple)):
        value = [json.loads(canonical_json(item)) for item in value]
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def load_strategy_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    validate_strategy_spec(spec)
    return spec


def load_complete_strategy_spec(path: Path = V2_SPEC_PATH) -> dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    validate_complete_strategy_spec(spec)
    return spec


def validate_strategy_spec(spec: Mapping[str, Any]) -> None:
    if spec.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ValueError("unexpected M4 strategy schema version")
    if spec.get("simulation_only") is not True:
        raise ValueError("M4 baseline strategy must remain simulation-only")
    strategy_version = str(spec.get("strategy_version") or "").strip()
    if not strategy_version:
        raise ValueError("strategy_version is required")
    weights = {
        name: _decimal(value, f"factor weight {name}", minimum=Decimal("0"))
        for name, value in dict(spec.get("factor_policy", {}).get("base_weights", {})).items()
    }
    if weights != BASE_WEIGHTS or sum(weights.values()) != Decimal("1"):
        raise ValueError("baseline factor weights differ from the preregistered roadmap")
    if spec["factor_policy"].get("qlib_owns_scoring_and_ranking") is not True:
        raise ValueError("Qlib must own factor scoring and ranking")
    flow_policy = spec["factor_policy"].get("flow_missing_policy", {})
    if flow_policy.get("zero_or_neutral_imputation_allowed") is not False:
        raise ValueError("missing verified flow cannot be encoded as zero or neutral evidence")
    caps = {name: _decimal(value, f"regime cap {name}") for name, value in spec["market_regime"]["gross_exposure_caps"].items()}
    if caps != REGIME_CAPS:
        raise ValueError("market-regime exposure caps differ from the roadmap")
    portfolio = spec.get("portfolio", {})
    expected_portfolio = {
        "minimum_holdings_when_sufficient_candidates": 8,
        "maximum_holdings": 12,
        "maximum_initial_stock_weight": Decimal("0.08"),
        "maximum_industry_weight": Decimal("0.20"),
        "maximum_planned_risk_per_stock": Decimal("0.005"),
        "maximum_planned_open_risk": Decimal("0.03"),
    }
    for field, expected in expected_portfolio.items():
        actual = portfolio.get(field)
        if isinstance(expected, Decimal):
            actual = _decimal(actual, field)
        if actual != expected:
            raise ValueError(f"portfolio rule {field} differs from the roadmap")
    unresolved = tuple(spec.get("unresolved_decision_rules") or ())
    if unresolved and spec.get("activation_state") != "disabled_preregistration":
        raise ValueError("a strategy with unresolved decision rules must remain disabled")


def validate_complete_strategy_spec(spec: Mapping[str, Any]) -> None:
    if spec.get("schema_version") != COMPLETE_SCHEMA_VERSION:
        raise ValueError("unexpected complete M4 strategy schema version")
    if spec.get("simulation_only") is not True or spec.get("activation_state") != "disabled_acceptance":
        raise ValueError("complete M4 strategy must remain disabled and simulation-only until acceptance")
    if spec.get("strategy_version") != "m4-a-share-adaptive-baseline-v2":
        raise ValueError("unexpected complete M4 strategy version")
    policy = spec.get("factor_policy", {})
    weights = {
        name: _decimal(value, f"factor weight {name}", minimum=Decimal("0"))
        for name, value in dict(policy.get("base_weights", {})).items()
    }
    if weights != BASE_WEIGHTS or sum(weights.values()) != Decimal("1"):
        raise ValueError("complete baseline factor weights differ from the roadmap")
    if policy.get("qlib_owns_dataset_scoring_and_ranking") is not True:
        raise ValueError("Qlib must own the complete baseline dataset, scoring, and ranking")
    if policy.get("flow_missing_policy", {}).get("zero_or_neutral_imputation_allowed") is not False:
        raise ValueError("complete strategy cannot impute missing verified flow")
    caps = {
        name: _decimal(value, f"regime cap {name}")
        for name, value in spec.get("market_regime", {}).get("gross_exposure_caps", {}).items()
    }
    if caps != REGIME_CAPS:
        raise ValueError("complete market-regime caps differ from the roadmap")
    research = spec.get("research_input", {})
    if research.get("adjusted_price_origin") != "rqalpha_public_history_bars_pre_adjusted":
        raise ValueError("price factors must use the RQAlpha public adjusted research view")
    if research.get("vendor_absolute_adjusted_prices_allowed") is not False:
        raise ValueError("vendor absolute adjusted prices cannot enter the complete strategy")
    if research.get("latest_release_lookup_allowed") is not False:
        raise ValueError("complete M4 inputs must never select latest releases implicitly")
    universe = spec.get("universe", {})
    if int(universe.get("minimum_listing_age_sessions", 0)) != 120:
        raise ValueError("listing-age gate differs from the roadmap")
    if int(universe.get("average_amount_window_sessions", 0)) != 20:
        raise ValueError("liquidity window differs from the roadmap")
    if _decimal(universe.get("average_amount_threshold_cny"), "amount threshold") != Decimal("100000000"):
        raise ValueError("liquidity threshold differs from the roadmap")
    portfolio = spec.get("portfolio", {})
    expected = {
        "minimum_holdings_when_sufficient_candidates": 8,
        "target_holdings": 10,
        "maximum_holdings": 12,
        "maximum_initial_stock_weight": Decimal("0.08"),
        "maximum_industry_weight": Decimal("0.20"),
        "maximum_planned_risk_per_stock": Decimal("0.005"),
        "maximum_planned_open_risk": Decimal("0.03"),
    }
    for field, expected_value in expected.items():
        actual: Any = portfolio.get(field)
        if isinstance(expected_value, Decimal):
            actual = _decimal(actual, field)
        if actual != expected_value:
            raise ValueError(f"complete portfolio rule {field} differs from the roadmap")


def effective_factor_weights(
    *,
    observed_factors: Sequence[str],
    flow_release_available: bool,
) -> dict[str, Decimal]:
    """Apply the preregistered missing-flow policy without neutral imputation."""
    observed = set(observed_factors)
    unknown = observed - set(FACTOR_NAMES)
    if unknown:
        raise ValueError(f"unknown factor observations: {sorted(unknown)}")
    required_non_flow = set(FACTOR_NAMES) - {"verified_capital_flow"}
    missing_required = sorted(required_non_flow - observed)
    if missing_required:
        raise ValueError(f"missing required factor observations: {missing_required}")
    if flow_release_available and "verified_capital_flow" not in observed:
        raise ValueError("candidate is missing verified flow from an available release")
    if flow_release_available:
        return dict(BASE_WEIGHTS)
    denominator = sum(BASE_WEIGHTS[name] for name in required_non_flow)
    result: dict[str, Decimal] = {}
    allocated = Decimal("0")
    ordered = [name for name in FACTOR_NAMES if name != "verified_capital_flow"]
    for name in ordered[:-1]:
        weight = (BASE_WEIGHTS[name] / denominator).quantize(_WEIGHT_QUANTUM, rounding=ROUND_DOWN)
        result[name] = weight
        allocated += weight
    result[ordered[-1]] = Decimal("1") - allocated
    result["verified_capital_flow"] = Decimal("0")
    return result


@dataclass(frozen=True, slots=True)
class FactorContribution:
    factor_name: str
    data_state: FactorDataState
    source_version: str | None
    standardized_score: Decimal | None
    effective_weight: Decimal
    contribution: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.data_state, FactorDataState):
            raise ValueError("factor data_state must use the typed FactorDataState enum")
        if self.factor_name not in FACTOR_NAMES:
            raise ValueError(f"unknown factor: {self.factor_name}")
        weight = _decimal(self.effective_weight, "effective_weight", minimum=Decimal("0"))
        contribution = _decimal(self.contribution, "contribution")
        object.__setattr__(self, "effective_weight", weight)
        object.__setattr__(self, "contribution", contribution)
        if self.data_state is FactorDataState.OBSERVED:
            if not self.source_version or self.standardized_score is None:
                raise ValueError("observed factor requires source version and standardized score")
            score = _decimal(self.standardized_score, "standardized_score")
            object.__setattr__(self, "standardized_score", score)
            if contribution != score * weight:
                raise ValueError("factor contribution must equal standardized score times effective weight")
        elif self.data_state is FactorDataState.DISABLED_RELEASE_WIDE:
            if self.standardized_score is not None or weight != 0 or contribution != 0:
                raise ValueError("disabled factor must carry no score, weight, or contribution")
        else:
            if self.standardized_score is not None or weight != 0 or contribution != 0:
                raise ValueError("missing factor must carry no score, weight, or contribution")


@dataclass(frozen=True, slots=True)
class BaselineRecommendation:
    recommendation_id: str
    symbol: str
    business_date: date
    valid_until: date
    data_version: str
    strategy_version: str
    regime: MarketRegime
    actionability_state: ActionabilityState
    reason_codes: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    factor_contributions: tuple[FactorContribution, ...]
    factor_score: Decimal | None
    account_equity: Decimal
    planned_loss: Decimal
    planned_weight: Decimal
    resulting_industry_weight: Decimal
    total_planned_open_risk: Decimal
    entry_price_lower: Decimal | None = None
    entry_price_upper: Decimal | None = None
    stop_price: Decimal | None = None
    defensive_price: Decimal | None = None
    target_prices: tuple[Decimal, ...] = ()


def validate_recommendation(recommendation: BaselineRecommendation, spec: Mapping[str, Any]) -> None:
    validate_strategy_spec(spec)
    if not isinstance(recommendation.business_date, date) or not isinstance(recommendation.valid_until, date):
        raise ValueError("recommendation business_date and valid_until must be typed dates")
    if not isinstance(recommendation.regime, MarketRegime):
        raise ValueError("recommendation regime must use the typed MarketRegime enum")
    if not isinstance(recommendation.actionability_state, ActionabilityState):
        raise ValueError("recommendation actionability must use the typed ActionabilityState enum")
    if not recommendation.recommendation_id.strip() or not _SYMBOL.fullmatch(recommendation.symbol):
        raise ValueError("recommendation requires an id and six-digit A-share symbol")
    if recommendation.valid_until < recommendation.business_date:
        raise ValueError("recommendation validity cannot precede its business date")
    if not recommendation.data_version.strip() or recommendation.strategy_version != spec["strategy_version"]:
        raise ValueError("recommendation data and strategy versions must be explicit and exact")
    if not recommendation.reason_codes or any(not str(code).strip() for code in recommendation.reason_codes):
        raise ValueError("recommendation requires at least one structured reason code")
    factors = recommendation.factor_contributions
    if tuple(row.factor_name for row in factors) != FACTOR_NAMES:
        raise ValueError("factor contributions must use the canonical deterministic order")
    missing_factors = tuple(
        row.factor_name for row in factors
        if row.data_state in {FactorDataState.MISSING_REQUIRED, FactorDataState.MISSING_CANDIDATE}
    )
    observed_weight = sum(row.effective_weight for row in factors)
    if missing_factors:
        if recommendation.factor_score is not None:
            raise ValueError("recommendation with missing factors cannot carry a comparable factor score")
        if recommendation.actionability_state is ActionabilityState.ACTIONABLE:
            raise ValueError("recommendation with missing factors cannot be actionable")
    else:
        if observed_weight != Decimal("1"):
            raise ValueError("effective factor weights must sum exactly to one")
        score = _decimal(recommendation.factor_score, "factor_score")
        if score != sum(row.contribution for row in factors):
            raise ValueError("factor score must equal the recorded contribution sum")
    equity = _decimal(recommendation.account_equity, "account_equity", minimum=Decimal("0.0001"))
    planned_loss = _decimal(recommendation.planned_loss, "planned_loss", minimum=Decimal("0"))
    planned_weight = _decimal(recommendation.planned_weight, "planned_weight", minimum=Decimal("0"))
    industry_weight = _decimal(recommendation.resulting_industry_weight, "resulting_industry_weight", minimum=Decimal("0"))
    open_risk = _decimal(recommendation.total_planned_open_risk, "total_planned_open_risk", minimum=Decimal("0"))
    portfolio = spec["portfolio"]
    if planned_loss > equity * _decimal(portfolio["maximum_planned_risk_per_stock"], "risk cap"):
        raise ValueError("planned stock loss exceeds 0.5% of account equity")
    if planned_weight > _decimal(portfolio["maximum_initial_stock_weight"], "stock weight cap"):
        raise ValueError("planned stock weight exceeds 8%")
    if industry_weight > _decimal(portfolio["maximum_industry_weight"], "industry weight cap"):
        raise ValueError("resulting industry weight exceeds 20%")
    if open_risk > _decimal(portfolio["maximum_planned_open_risk"], "open risk cap"):
        raise ValueError("total planned open risk exceeds 3%")
    if planned_weight > _decimal(spec["market_regime"]["gross_exposure_caps"][recommendation.regime.value], "regime cap"):
        raise ValueError("planned stock weight exceeds the market-regime gross exposure cap")
    if recommendation.actionability_state is ActionabilityState.ACTIONABLE:
        if spec["activation_state"] != "active" or spec.get("unresolved_decision_rules"):
            raise ValueError("preregistered M4.1 contract is disabled and cannot emit actionable recommendations")
        if recommendation.blocked_reasons:
            raise ValueError("actionable recommendation cannot carry blocked reasons")
        validate_actionable_price_relationships(recommendation)
    elif not recommendation.blocked_reasons:
        raise ValueError("blocked recommendation requires an exact blocked reason")


def validate_actionable_price_relationships(recommendation: BaselineRecommendation) -> None:
    names = ("entry_price_lower", "entry_price_upper", "stop_price", "defensive_price")
    if any(getattr(recommendation, name) is None for name in names) or not recommendation.target_prices:
        raise ValueError("actionable recommendation is missing required structured price fields")
    lower = _decimal(recommendation.entry_price_lower, "entry_price_lower", minimum=Decimal("0.0001"))
    upper = _decimal(recommendation.entry_price_upper, "entry_price_upper", minimum=Decimal("0.0001"))
    stop = _decimal(recommendation.stop_price, "stop_price", minimum=Decimal("0.0001"))
    defensive = _decimal(recommendation.defensive_price, "defensive_price", minimum=Decimal("0.0001"))
    targets = tuple(_decimal(value, "target_price", minimum=Decimal("0.0001")) for value in recommendation.target_prices)
    if not stop < defensive <= lower <= upper:
        raise ValueError("required price relationship is stop < defensive <= entry lower <= entry upper")
    if tuple(sorted(set(targets))) != targets or targets[0] <= upper:
        raise ValueError("target prices must be unique, ascending, and above the entry range")


def render_recommendation_narrative(recommendation: BaselineRecommendation) -> str:
    """Render text one-way from validated fields; it is never an execution input."""
    reasons = ", ".join(recommendation.reason_codes)
    if recommendation.actionability_state is ActionabilityState.BLOCKED:
        return f"{recommendation.symbol} is blocked: {', '.join(recommendation.blocked_reasons)}; reasons: {reasons}."
    targets = ", ".join(format(value, "f") for value in recommendation.target_prices)
    return (
        f"{recommendation.symbol} entry {recommendation.entry_price_lower}-{recommendation.entry_price_upper}; "
        f"stop {recommendation.stop_price}; targets {targets}; reasons: {reasons}."
    )

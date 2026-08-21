"""Transparent M4 factor preparation; Qlib remains the scoring owner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

import numpy as np
import pandas as pd

from scripts.strategy.baseline_contracts import FACTOR_NAMES
from scripts.strategy.research_contracts import (
    ComponentState,
    M4ResearchRelease,
    validate_research_frame,
)


@dataclass(frozen=True, slots=True)
class FactorResearchResult:
    business_date: date
    universe: pd.DataFrame
    features: pd.DataFrame
    flow_available: bool
    source_versions: tuple[tuple[str, str], ...]


def _last(group: pd.DataFrame, column: str):
    return group[column].iloc[-1]


def evaluate_universe(frame: pd.DataFrame, *, business_date: date) -> pd.DataFrame:
    """Evaluate point-in-time eligibility without silently dropping reasons."""
    through = frame.loc[frame.index.get_level_values("datetime").date <= business_date]
    records: list[dict[str, object]] = []
    raw_volatility: dict[str, float] = {}
    groups = {str(symbol): rows.droplevel("instrument").sort_index() for symbol, rows in through.groupby(level="instrument")}
    for symbol, rows in groups.items():
        reasons: list[str] = []
        if rows.empty or rows.index[-1].date() != business_date:
            reasons.append("MISSING_BUSINESS_DATE_ROW")
        if len(rows) < 120:
            reasons.append("INSUFFICIENT_PRICE_HISTORY")
        current = rows.iloc[-1]
        if float(rows["amount_cny"].tail(20).mean()) < 100_000_000:
            reasons.append("LIQUIDITY_BELOW_THRESHOLD")
        if int(current["listing_age_sessions"]) < 120:
            reasons.append("LISTING_AGE_BELOW_120")
        for column, code in (
            ("is_st", "ST"), ("delisting_risk", "DELISTING_RISK"),
            ("is_suspended", "SUSPENDED"), ("one_price_limit_up", "ONE_PRICE_LIMIT_UP"),
            ("one_price_limit_down", "ONE_PRICE_LIMIT_DOWN"),
        ):
            if bool(current[column]):
                reasons.append(code)
        if not bool(current["can_buy"]):
            reasons.append("CANNOT_BUY")
        limit_down = rows["at_limit_down"].tail(10).astype(bool).to_numpy()
        if any(limit_down[index - 1] and limit_down[index] for index in range(1, len(limit_down))):
            reasons.append("CONSECUTIVE_LIMIT_DOWN")
        returns = np.log(rows["adjusted_close"].astype(float)).diff().tail(60).dropna()
        raw_volatility[symbol] = float(returns.std(ddof=0)) if len(returns) >= 59 else np.nan
        records.append({
            "instrument": symbol,
            "industry_level1": str(current["industry_level1"]),
            "volatility_60": raw_volatility[symbol],
            "blocked_reasons": tuple(sorted(set(reasons))),
        })
    result = pd.DataFrame.from_records(records).set_index("instrument").sort_index()
    valid_vol = result["volatility_60"].dropna()
    threshold = valid_vol.quantile(0.95) if not valid_vol.empty else np.nan
    if np.isfinite(threshold):
        for symbol in result.index[result["volatility_60"] > threshold]:
            result.at[symbol, "blocked_reasons"] = tuple(sorted((*result.at[symbol, "blocked_reasons"], "VOLATILITY_TOP_5_PERCENT")))
    result["eligible"] = result["blocked_reasons"].map(len).eq(0)
    return result


def _zscore(values: pd.Series) -> pd.Series:
    standard = float(values.std(ddof=0))
    if not np.isfinite(standard) or standard <= 1e-12:
        raise ValueError("factor has no cross-sectional dispersion")
    return (values - float(values.mean())) / standard


def _neutralize(values: pd.Series, industry: pd.Series, assets: pd.Series) -> pd.Series:
    joined = pd.concat({"factor": values, "industry": industry, "assets": assets}, axis=1).dropna()
    if len(joined) < 8 or (joined["assets"] <= 0).any():
        raise ValueError("factor neutralization requires at least eight valid positive-asset observations")
    lower, upper = joined["factor"].quantile([0.01, 0.99])
    y = joined["factor"].clip(lower, upper).astype(float)
    counts = joined["industry"].astype(str).value_counts()
    labels = joined["industry"].astype(str).where(joined["industry"].astype(str).map(counts) >= 5, "__SMALL__")
    dummies = pd.get_dummies(labels, prefix="industry", dtype=float, drop_first=True)
    design = pd.concat([
        pd.Series(1.0, index=joined.index, name="intercept"),
        np.log(joined["assets"].astype(float)).rename("log_assets"),
        dummies,
    ], axis=1)
    coefficients, *_ = np.linalg.lstsq(design.to_numpy(float), y.to_numpy(float), rcond=None)
    residual = y - design.to_numpy(float) @ coefficients
    return _zscore(pd.Series(residual, index=joined.index, dtype=float))


def _raw_symbol_factors(rows: pd.DataFrame, *, flow_available: bool) -> Mapping[str, float]:
    rows = rows.sort_index()
    if len(rows) < 121:
        raise ValueError("factor calculation requires at least 121 sessions")
    close = rows["adjusted_close"].astype(float)
    log_close = np.log(close)
    momentum = float(log_close.iloc[-21] - log_close.iloc[-121])
    trend = log_close.tail(60).to_numpy(float)
    x = np.arange(60, dtype=float)
    design = np.column_stack([np.ones(60), x])
    coefficients, *_ = np.linalg.lstsq(design, trend, rcond=None)
    residual_std = max(float(np.std(trend - design @ coefficients, ddof=0)), 1e-12)
    trend_quality = float(coefficients[1] / residual_std)
    amount20 = rows["amount_cny"].tail(20).astype(float)
    turnover20 = rows["turnover_percent"].tail(20).astype(float)
    if turnover20.isna().any() or (turnover20 < 0).any():
        raise ValueError("turnover window is incomplete or invalid")
    turnover_mean = float(turnover20.mean())
    stability = -float(turnover20.std(ddof=0) / max(turnover_mean, 1e-12))
    return20 = log_close.diff().tail(20)
    correlation = float(return20.corr(np.log1p(turnover20)))
    if not np.isfinite(correlation):
        raise ValueError("price-turnover correlation is undefined")
    roe = float(_last(rows, "parent_netprofit_ttm") / _last(rows, "average_parent_equity"))
    cash_conversion = float(_last(rows, "netcash_operate_ttm") / max(abs(_last(rows, "parent_netprofit_ttm")), 1e-12))
    inverse_leverage = float(1 - _last(rows, "total_liabilities") / _last(rows, "total_assets"))
    stability_earnings = -float(_last(rows, "earnings_variability_8q"))
    result = {
        "residual_momentum": momentum,
        "trend_quality": trend_quality,
        "amount_component": float(np.log(amount20.mean())),
        "turnover_stability_component": stability,
        "price_turnover_correlation_component": correlation,
        "roe_component": roe,
        "cash_conversion_component": cash_conversion,
        "inverse_leverage_component": inverse_leverage,
        "earnings_stability_component": stability_earnings,
    }
    if flow_available:
        flow = rows["main_net_inflow_cny"].tail(5)
        if flow.isna().any():
            raise ValueError("verified-flow release is missing a candidate window")
        result["verified_capital_flow"] = float(flow.sum() / rows["amount_cny"].tail(5).sum())
    return result


def _atr20(rows: pd.DataFrame) -> float:
    window = rows.sort_index().tail(21)
    if len(window) < 21:
        raise ValueError("ATR20 requires 21 adjusted-price observations")
    previous_close = window["adjusted_close"].astype(float).shift(1)
    true_range = pd.concat([
        window["adjusted_high"].astype(float) - window["adjusted_low"].astype(float),
        (window["adjusted_high"].astype(float) - previous_close).abs(),
        (window["adjusted_low"].astype(float) - previous_close).abs(),
    ], axis=1).max(axis=1).iloc[1:]
    atr = float(true_range.mean())
    if not np.isfinite(atr) or atr <= 0:
        raise ValueError("ATR20 is invalid")
    return atr


def build_factor_research(
    frame: pd.DataFrame, *, release: M4ResearchRelease
) -> FactorResearchResult:
    """Build neutralized factor features but deliberately do not score them."""
    validate_research_frame(frame, release)
    if not release.actionable_research_ready:
        raise ValueError("required M4 research components are not accepted")
    universe = evaluate_universe(frame, business_date=release.business_date)
    eligible = universe.index[universe["eligible"]].tolist()
    if len(eligible) < 8:
        raise ValueError("fewer than eight eligible candidates; full cash is required")
    flow_available = release.component("flow").state is ComponentState.ACCEPTED
    records: dict[str, Mapping[str, float]] = {}
    current: dict[str, pd.Series] = {}
    for symbol in eligible:
        rows = frame.xs(symbol, level="instrument").sort_index()
        records[symbol] = _raw_symbol_factors(rows, flow_available=flow_available)
        current[symbol] = rows.iloc[-1]
    raw = pd.DataFrame.from_dict(records, orient="index").sort_index()
    raw["volume_price_liquidity"] = (
        _zscore(raw["amount_component"]) * 0.40
        + _zscore(raw["turnover_stability_component"]) * 0.30
        + _zscore(raw["price_turnover_correlation_component"]) * 0.30
    )
    raw["quality_risk"] = (
        _zscore(raw["roe_component"]) * 0.35
        + _zscore(raw["cash_conversion_component"]) * 0.25
        + _zscore(raw["inverse_leverage_component"]) * 0.20
        + _zscore(raw["earnings_stability_component"]) * 0.20
    )
    industry = pd.Series({symbol: row["industry_level1"] for symbol, row in current.items()})
    assets = pd.Series({symbol: float(row["total_assets"]) for symbol, row in current.items()})
    output = pd.DataFrame(index=raw.index)
    for factor in FACTOR_NAMES:
        if factor == "verified_capital_flow" and not flow_available:
            output[factor] = np.nan
        else:
            output[factor] = _neutralize(raw[factor], industry, assets).reindex(output.index)
    output["industry_level1"] = industry
    output["total_assets"] = assets
    output["current_price"] = pd.Series({symbol: float(row["adjusted_close"]) for symbol, row in current.items()})
    output["atr20"] = pd.Series({
        symbol: _atr20(frame.xs(symbol, level="instrument")) for symbol in eligible
    })
    output.index = pd.MultiIndex.from_arrays(
        [pd.to_datetime([release.business_date] * len(output)), output.index],
        names=("datetime", "instrument"),
    )
    versions = tuple((component.name, component.dataset_id) for component in release.components)
    return FactorResearchResult(release.business_date, universe, output.sort_index(), flow_available, versions)

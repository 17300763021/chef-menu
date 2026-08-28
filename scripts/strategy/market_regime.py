"""Deterministic, fail-closed market-regime classification for M4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pandas as pd

from scripts.strategy.baseline_contracts import MarketRegime, REGIME_CAPS


REQUIRED_INDEXES = ("000300", "000905")


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    business_date: date
    regime: MarketRegime
    gross_exposure_cap: Decimal
    breadth: Decimal | None
    index_metrics: tuple[tuple[str, Decimal, Decimal, Decimal, Decimal, Decimal], ...]
    reason_codes: tuple[str, ...]
    data_valid: bool


def _critical(business_date: date, reason: str) -> RegimeAssessment:
    return RegimeAssessment(
        business_date=business_date,
        regime=MarketRegime.CRITICAL_RISK,
        gross_exposure_cap=REGIME_CAPS[MarketRegime.CRITICAL_RISK.value],
        breadth=None,
        index_metrics=(),
        reason_codes=(reason,),
        data_valid=False,
    )


def classify_market_regime(
    index_bars: pd.DataFrame,
    *,
    business_date: date,
    breadth: Decimal | float | str | None,
) -> RegimeAssessment:
    """Apply the preregistered ordered rules; bad evidence becomes critical risk."""
    required = {"datetime", "index_code", "close"}
    if not required.issubset(index_bars.columns):
        return _critical(business_date, "REGIME_INDEX_SCHEMA_INVALID")
    frame = index_bars.loc[:, ["datetime", "index_code", "close"]].copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame["index_code"] = frame["index_code"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if frame.isna().any().any() or frame["datetime"].dt.tz is not None:
        return _critical(business_date, "REGIME_INDEX_VALUES_INVALID")
    if frame.duplicated(["datetime", "index_code"]).any():
        return _critical(business_date, "REGIME_INDEX_DUPLICATE")
    if frame["datetime"].dt.date.max() > business_date:
        return _critical(business_date, "REGIME_INDEX_FUTURE_DATA")
    try:
        breadth_value = Decimal(str(breadth))
    except Exception:
        return _critical(business_date, "REGIME_BREADTH_INVALID")
    if not breadth_value.is_finite() or not Decimal("0") <= breadth_value <= Decimal("1"):
        return _critical(business_date, "REGIME_BREADTH_INVALID")

    metrics: list[tuple[str, Decimal, Decimal, Decimal, Decimal, Decimal]] = []
    for code in REQUIRED_INDEXES:
        rows = frame.loc[frame["index_code"] == code].sort_values("datetime")
        rows = rows.loc[rows["datetime"].dt.date <= business_date]
        if len(rows) < 120 or rows.iloc[-1]["datetime"].date() != business_date:
            return _critical(business_date, f"REGIME_INDEX_HISTORY_INCOMPLETE:{code}")
        closes = rows["close"].astype(float)
        if (closes <= 0).any():
            return _critical(business_date, f"REGIME_INDEX_PRICE_INVALID:{code}")
        close = Decimal(str(closes.iloc[-1]))
        ma20 = Decimal(str(closes.tail(20).mean()))
        ma60 = Decimal(str(closes.tail(60).mean()))
        ma120 = Decimal(str(closes.tail(120).mean()))
        peak20 = Decimal(str(closes.tail(20).max()))
        drawdown20 = close / peak20 - Decimal("1")
        metrics.append((code, close, ma20, ma60, ma120, drawdown20))

    worst_drawdown = min(row[5] for row in metrics)
    above_120 = all(row[1] > row[4] for row in metrics)
    below_120 = all(row[1] < row[4] for row in metrics)
    short_trend = all(row[2] > row[3] for row in metrics)
    if worst_drawdown <= Decimal("-0.10") or breadth_value <= Decimal("0.20"):
        regime = MarketRegime.CRITICAL_RISK
        reasons = ("CRITICAL_DRAWDOWN_OR_BREADTH",)
    elif above_120 and short_trend and breadth_value >= Decimal("0.65"):
        regime = MarketRegime.STRONG_BULL
        reasons = ("BOTH_INDEXES_STRONG_AND_BREADTH_HIGH",)
    elif above_120 and breadth_value >= Decimal("0.50"):
        regime = MarketRegime.WEAK_BULL
        reasons = ("BOTH_INDEXES_ABOVE_MA120",)
    elif below_120 and breadth_value < Decimal("0.40"):
        regime = MarketRegime.BEAR
        reasons = ("BOTH_INDEXES_BELOW_MA120",)
    else:
        regime = MarketRegime.RANGE
        reasons = ("ORDERED_RULE_FALLTHROUGH",)
    return RegimeAssessment(
        business_date=business_date,
        regime=regime,
        gross_exposure_cap=REGIME_CAPS[regime.value],
        breadth=breadth_value,
        index_metrics=tuple(metrics),
        reason_codes=reasons,
        data_valid=True,
    )

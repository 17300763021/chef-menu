from __future__ import annotations

import unittest
from importlib.util import find_spec
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from scripts.strategy.baseline_contracts import FACTOR_NAMES, MarketRegime, load_complete_strategy_spec
from scripts.strategy.baseline_factors import build_factor_research
from scripts.strategy.market_regime import RegimeAssessment, classify_market_regime
from scripts.strategy.portfolio_research import (
    Candidate,
    OpenPosition,
    ResearchDecision,
    build_portfolio_research_plan,
    candidates_from_qlib_scores,
)
from scripts.strategy.qlib_adapter import score_and_rank_with_qlib
from scripts.strategy.research_contracts import (
    ComponentState,
    M4ResearchRelease,
    REQUIRED_COMPONENTS,
    RQALPHA_ADJUSTED_ORIGIN,
    ResearchComponent,
    validate_research_frame,
)


BUSINESS_DATE = date(2026, 7, 31)
STRATEGY_VERSION = "m4-a-share-adaptive-baseline-v2"
HASH = "a" * 64


def release(*, fundamental_state: ComponentState = ComponentState.ACCEPTED) -> M4ResearchRelease:
    states = {name: ComponentState.ACCEPTED for name in REQUIRED_COMPONENTS}
    states["fundamental"] = fundamental_state
    states["flow"] = ComponentState.DISABLED_OPTIONAL
    components = []
    for name in REQUIRED_COMPONENTS:
        available = 0 if name == "flow" else (980 if name == "fundamental" else 1000)
        components.append(ResearchComponent(
            name=name, dataset_id=f"m2-{name}-20260731-v1", manifest_sha256=HASH,
            through_date=BUSINESS_DATE, state=states[name], expected_count=1000,
            available_count=available,
        ))
    return M4ResearchRelease("m4-release-20260731-v1", BUSINESS_DATE, STRATEGY_VERSION, tuple(components))


def research_frame(symbol_count: int = 12) -> pd.DataFrame:
    rows = []
    for day_index in range(121):
        session = BUSINESS_DATE - timedelta(days=120 - day_index)
        for symbol_index in range(symbol_count):
            symbol = f"{600000 + symbol_index:06d}"
            trend = 10 + symbol_index * 0.7 + day_index * (0.015 + symbol_index * 0.0007)
            wave = np.sin(day_index / (4 + symbol_index / 4)) * (0.04 + symbol_index * 0.003)
            close = trend + wave
            rows.append({
                "datetime": pd.Timestamp(session), "instrument": symbol,
                "adjusted_high": close * 1.01, "adjusted_low": close * 0.99,
                "adjusted_close": close, "raw_close": close,
                "amount_cny": 150_000_000 + symbol_index * 9_000_000 + day_index * 50_000,
                "turnover_percent": 1 + symbol_index * 0.08 + np.sin(day_index / 7) * 0.1,
                "industry_level1": f"I{symbol_index // 6}",
                "total_assets": 10_000_000_000 + symbol_index * 900_000_000,
                "total_liabilities": 4_000_000_000 + symbol_index * 200_000_000,
                "average_parent_equity": 5_000_000_000 + symbol_index * 300_000_000,
                "parent_netprofit_ttm": 300_000_000 + symbol_index * 40_000_000,
                "netcash_operate_ttm": 250_000_000 + symbol_index * 35_000_000,
                "earnings_variability_8q": 0.5 - symbol_index * 0.02,
                "main_net_inflow_cny": np.nan, "listing_age_sessions": 500,
                "is_st": False, "delisting_risk": False, "is_suspended": False,
                "one_price_limit_up": False, "one_price_limit_down": False,
                "at_limit_down": False, "can_buy": True, "can_sell": True,
                "adjusted_price_origin": RQALPHA_ADJUSTED_ORIGIN,
                "source_row_sha256": f"{symbol_index:064x}",
            })
    return pd.DataFrame(rows).set_index(["datetime", "instrument"]).sort_index()


class M4StrategyResearchTests(unittest.TestCase):
    def test_complete_spec_freezes_framework_boundaries(self):
        spec = load_complete_strategy_spec()
        self.assertEqual("rqalpha_public_history_bars_pre_adjusted", spec["research_input"]["adjusted_price_origin"])
        self.assertTrue(spec["factor_policy"]["qlib_owns_dataset_scoring_and_ranking"])
        self.assertEqual("disabled_acceptance", spec["activation_state"])

    def test_research_release_blocks_partial_fundamentals(self):
        partial = release(fundamental_state=ComponentState.INCOMPLETE)
        self.assertFalse(partial.actionable_research_ready)
        with self.assertRaisesRegex(ValueError, "not accepted"):
            build_factor_research(research_frame(), release=partial)

    def test_research_frame_rejects_vendor_adjusted_data(self):
        frame = research_frame()
        frame["qfq_close"] = frame["adjusted_close"]
        with self.assertRaisesRegex(ValueError, "vendor adjusted"):
            validate_research_frame(frame, release())

    def test_transparent_factors_are_deterministic_and_flow_is_disabled(self):
        first = build_factor_research(research_frame(), release=release())
        second = build_factor_research(research_frame(), release=release())
        pd.testing.assert_frame_equal(first.features, second.features)
        self.assertFalse(first.flow_available)
        self.assertGreaterEqual(len(first.features), 8)
        self.assertTrue(first.features["verified_capital_flow"].isna().all())
        self.assertFalse(first.features[list(set(FACTOR_NAMES) - {"verified_capital_flow"})].isna().any().any())

    def test_invalid_regime_data_fails_to_zero_exposure(self):
        result = classify_market_regime(pd.DataFrame(), business_date=BUSINESS_DATE, breadth=Decimal("0.80"))
        self.assertEqual(MarketRegime.CRITICAL_RISK, result.regime)
        self.assertEqual(Decimal("0"), result.gross_exposure_cap)
        self.assertFalse(result.data_valid)

    def test_regime_order_is_deterministic(self):
        rows = []
        for code in ("000300", "000905"):
            for index in range(120):
                rows.append({
                    "datetime": pd.Timestamp(BUSINESS_DATE - timedelta(days=119 - index)),
                    "index_code": code, "close": 100 + index,
                })
        result = classify_market_regime(pd.DataFrame(rows), business_date=BUSINESS_DATE, breadth=Decimal("0.70"))
        self.assertEqual(MarketRegime.STRONG_BULL, result.regime)
        self.assertEqual(Decimal("0.80"), result.gross_exposure_cap)

    def test_no_non_qlib_scoring_fallback(self):
        features = build_factor_research(research_frame(), release=release()).features
        if find_spec("qlib") is None:
            with self.assertRaisesRegex(RuntimeError, "no non-Qlib scoring fallback"):
                score_and_rank_with_qlib(features, flow_available=False)
        else:
            first = score_and_rank_with_qlib(features, flow_available=False)
            second = score_and_rank_with_qlib(features, flow_available=False)
            pd.testing.assert_frame_equal(first.scores, second.scores)
            self.assertIn("DatasetH", first.qlib_dataset_type)

    def test_portfolio_covers_positions_and_enforces_caps(self):
        regime = RegimeAssessment(BUSINESS_DATE, MarketRegime.RANGE, Decimal("0.40"), Decimal("0.55"), (), ("RANGE",), True)
        position = OpenPosition(
            "600001", "I0", Decimal("0.05"), Decimal("0.003"), Decimal("9"), Decimal("8.5"),
            Decimal("10"), 21, Decimal("-0.02"), Decimal("0.01"), Decimal("0.40"), Decimal("0.45"),
        )
        values = tuple((name, None if name == "verified_capital_flow" else Decimal("1")) for name in FACTOR_NAMES)
        candidates = [Candidate(
            f"{600010 + index:06d}", f"I{index % 3}", Decimal(str(10 - index)), index + 1,
            Decimal("0.9") - Decimal(index) / Decimal("100"), Decimal("20"), Decimal("1"), values,
        ) for index in range(10)]
        plan = build_portfolio_research_plan(
            business_date=BUSINESS_DATE, strategy_version=STRATEGY_VERSION,
            research_release_id="m4-release-20260731-v1", regime=regime,
            positions=(position,), candidates=candidates,
        )
        holding = next(row for row in plan.recommendations if row.symbol == "600001")
        self.assertEqual(ResearchDecision.EXIT_REVIEW, holding.decision)
        selected = [row for row in plan.recommendations if row.decision is ResearchDecision.NEW_RESEARCH_CANDIDATE]
        self.assertTrue(plan.complete_position_coverage)
        self.assertTrue(all(row.planned_weight <= Decimal("0.08") for row in selected))
        self.assertLessEqual(sum(row.planned_risk for row in selected) + position.planned_risk, Decimal("0.03"))
        self.assertTrue(all(not row.simulation_order_allowed for row in plan.recommendations))

    def test_qlib_scores_join_one_for_one_without_rescoring(self):
        features = build_factor_research(research_frame(), release=release()).features
        scores = pd.DataFrame(index=features.index)
        scores["factor_score"] = np.linspace(1, 0, len(scores))
        scores["rank"] = range(1, len(scores) + 1)
        scores["percentile"] = 1 - (scores["rank"] - 1) / len(scores)
        candidates = candidates_from_qlib_scores(features, scores)
        self.assertEqual(len(features), len(candidates))
        self.assertEqual(Decimal(str(scores.iloc[0]["factor_score"])), candidates[0].factor_score)
        with self.assertRaisesRegex(ValueError, "one-for-one"):
            candidates_from_qlib_scores(features.iloc[:-1], scores)


if __name__ == "__main__":
    unittest.main()

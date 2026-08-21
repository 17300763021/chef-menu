"""Deterministic acceptance tests for the disabled M4.1 strategy contract."""

from __future__ import annotations

import copy
import unittest
from datetime import date
from decimal import Decimal

from scripts.strategy.baseline_contracts import (
    FACTOR_NAMES,
    SPEC_PATH,
    ActionabilityState,
    BaselineRecommendation,
    FactorContribution,
    FactorDataState,
    MarketRegime,
    content_sha256,
    effective_factor_weights,
    load_strategy_spec,
    render_recommendation_narrative,
    validate_actionable_price_relationships,
    validate_recommendation,
    validate_strategy_spec,
)


def _contributions(flow_available: bool = False) -> tuple[FactorContribution, ...]:
    observed = [name for name in FACTOR_NAMES if name != "verified_capital_flow" or flow_available]
    weights = effective_factor_weights(observed_factors=observed, flow_release_available=flow_available)
    rows = []
    for name in FACTOR_NAMES:
        if name == "verified_capital_flow" and not flow_available:
            rows.append(FactorContribution(name, FactorDataState.DISABLED_RELEASE_WIDE, None, None, Decimal("0"), Decimal("0")))
        else:
            rows.append(FactorContribution(name, FactorDataState.OBSERVED, "fixture-v1", Decimal("1"), weights[name], weights[name]))
    return tuple(rows)


def _blocked(**overrides):
    values = {
        "recommendation_id": "recommendation-fixture",
        "symbol": "600519",
        "business_date": date(2026, 7, 28),
        "valid_until": date(2026, 7, 29),
        "data_version": "m2-release-fixture",
        "strategy_version": "m4-a-share-adaptive-baseline-v1-contract-only",
        "regime": MarketRegime.CRITICAL_RISK,
        "actionability_state": ActionabilityState.BLOCKED,
        "reason_codes": ("contract_only",),
        "blocked_reasons": ("unresolved_decision_rules",),
        "factor_contributions": _contributions(),
        "factor_score": Decimal("1"),
        "account_equity": Decimal("1000000"),
        "planned_loss": Decimal("0"),
        "planned_weight": Decimal("0"),
        "resulting_industry_weight": Decimal("0"),
        "total_planned_open_risk": Decimal("0"),
    }
    values.update(overrides)
    return BaselineRecommendation(**values)


class M4BaselineContractTests(unittest.TestCase):
    def test_spec_is_stable_simulation_only_and_disabled(self):
        first = load_strategy_spec()
        second = load_strategy_spec(SPEC_PATH)
        self.assertEqual(content_sha256(first), content_sha256(second))
        self.assertTrue(first["simulation_only"])
        self.assertEqual(first["activation_state"], "disabled_preregistration")
        self.assertTrue(first["unresolved_decision_rules"])

    def test_unresolved_contract_cannot_be_activated(self):
        spec = copy.deepcopy(load_strategy_spec())
        spec["activation_state"] = "active"
        with self.assertRaisesRegex(ValueError, "unresolved decision rules"):
            validate_strategy_spec(spec)

    def test_release_wide_missing_flow_disables_instead_of_imputing(self):
        weights = effective_factor_weights(
            observed_factors=[name for name in FACTOR_NAMES if name != "verified_capital_flow"],
            flow_release_available=False,
        )
        self.assertEqual(weights["verified_capital_flow"], Decimal("0"))
        self.assertEqual(sum(weights.values()), Decimal("1"))
        self.assertNotEqual(weights["residual_momentum"], Decimal("0.25"))

    def test_candidate_missing_flow_from_available_release_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "candidate is missing verified flow"):
            effective_factor_weights(
                observed_factors=[name for name in FACTOR_NAMES if name != "verified_capital_flow"],
                flow_release_available=True,
            )

    def test_missing_candidate_factor_is_preserved_as_blocked_evidence(self):
        factors = list(_contributions())
        flow_index = FACTOR_NAMES.index("verified_capital_flow")
        factors[flow_index] = FactorContribution(
            "verified_capital_flow", FactorDataState.MISSING_CANDIDATE,
            "flow-release-v1", None, Decimal("0"), Decimal("0"),
        )
        recommendation = _blocked(
            factor_contributions=tuple(factors),
            factor_score=None,
            blocked_reasons=("verified_flow_missing_for_candidate",),
        )
        validate_recommendation(recommendation, load_strategy_spec())
        self.assertIn("verified_flow_missing_for_candidate", render_recommendation_narrative(recommendation))

    def test_missing_factor_cannot_be_disguised_as_zero_score(self):
        with self.assertRaisesRegex(ValueError, "must carry no score"):
            FactorContribution(
                "verified_capital_flow", FactorDataState.MISSING_CANDIDATE,
                "flow-release-v1", Decimal("0"), Decimal("0"), Decimal("0"),
            )

    def test_blocked_recommendation_preserves_audit_fields_and_renders_one_way(self):
        recommendation = _blocked()
        validate_recommendation(recommendation, load_strategy_spec())
        narrative = render_recommendation_narrative(recommendation)
        self.assertIn("600519", narrative)
        self.assertIn("unresolved_decision_rules", narrative)

    def test_contract_only_strategy_cannot_emit_actionable_recommendation(self):
        recommendation = _blocked(
            actionability_state=ActionabilityState.ACTIONABLE,
            blocked_reasons=(),
            regime=MarketRegime.STRONG_BULL,
            planned_loss=Decimal("5000"),
            planned_weight=Decimal("0.08"),
            resulting_industry_weight=Decimal("0.20"),
            total_planned_open_risk=Decimal("0.03"),
            entry_price_lower=Decimal("100"),
            entry_price_upper=Decimal("102"),
            stop_price=Decimal("95"),
            defensive_price=Decimal("98"),
            target_prices=(Decimal("110"), Decimal("120")),
        )
        with self.assertRaisesRegex(ValueError, "disabled"):
            validate_recommendation(recommendation, load_strategy_spec())

    def test_actionable_price_relationships_are_structured_and_fail_closed(self):
        valid = _blocked(
            entry_price_lower=Decimal("100"),
            entry_price_upper=Decimal("102"),
            stop_price=Decimal("95"),
            defensive_price=Decimal("98"),
            target_prices=(Decimal("110"), Decimal("120")),
        )
        validate_actionable_price_relationships(valid)
        invalid = _blocked(
            entry_price_lower=Decimal("100"),
            entry_price_upper=Decimal("102"),
            stop_price=Decimal("101"),
            defensive_price=Decimal("98"),
            target_prices=(Decimal("110"),),
        )
        with self.assertRaisesRegex(ValueError, "stop < defensive"):
            validate_actionable_price_relationships(invalid)

    def test_risk_caps_fail_closed_even_for_blocked_output(self):
        with self.assertRaisesRegex(ValueError, "0.5%"):
            validate_recommendation(_blocked(planned_loss=Decimal("5000.01")), load_strategy_spec())
        with self.assertRaisesRegex(ValueError, "8%"):
            validate_recommendation(_blocked(planned_weight=Decimal("0.080001")), load_strategy_spec())
        with self.assertRaisesRegex(ValueError, "20%"):
            validate_recommendation(_blocked(resulting_industry_weight=Decimal("0.200001")), load_strategy_spec())
        with self.assertRaisesRegex(ValueError, "3%"):
            validate_recommendation(_blocked(total_planned_open_risk=Decimal("0.030001")), load_strategy_spec())

    def test_factor_contribution_must_reconcile(self):
        with self.assertRaisesRegex(ValueError, "must equal"):
            FactorContribution(
                "quality_risk", FactorDataState.OBSERVED, "fixture-v1",
                Decimal("2"), Decimal("0.25"), Decimal("0.49"),
            )

    def test_plain_string_cannot_impersonate_typed_factor_state(self):
        with self.assertRaisesRegex(ValueError, "typed FactorDataState"):
            FactorContribution(
                "quality_risk", "observed", "fixture-v1",  # type: ignore[arg-type]
                Decimal("1"), Decimal("0.25"), Decimal("0.25"),
            )

    def test_blocked_recommendation_requires_exact_reason(self):
        with self.assertRaisesRegex(ValueError, "exact blocked reason"):
            validate_recommendation(_blocked(blocked_reasons=()), load_strategy_spec())


if __name__ == "__main__":
    unittest.main()

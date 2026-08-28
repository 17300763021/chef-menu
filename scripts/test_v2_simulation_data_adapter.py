from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from scripts.market_data.manifest import sha256
from scripts.simulation.m2_data_source import (
    M2AdmissionPolicy,
    admit_daily_evidence,
    daily_release_id,
)


BUSINESS_DATE = date(2026, 7, 31)
SCOPE = "1" * 64
RELEASE_ID = daily_release_id(BUSINESS_DATE, SCOPE)
def _bar(symbol: str, close: str) -> dict[str, object]:
    return {
        "source": "akshare_eastmoney",
        "symbol": symbol,
        "exchange": "SSE",
        "business_date": BUSINESS_DATE.isoformat(),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "previous_close": None,
        "volume_shares": 10000,
        "amount_cny": "100000.00",
        "turnover_percent": "0.10",
        "trade_status": "trading",
        "is_st": False,
        "adjustment": "none",
        "schema_version": "m2-daily-bar-v1",
    }


def _fact(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "business_date": BUSINESS_DATE.isoformat(),
        "index_code": "000905",
        "has_primary_bar": True,
        "has_secondary_status": True,
        "is_suspended": False,
        "is_st": False,
        "listing_age_sessions": 100,
        "limit_rate": "0.10",
        "limit_up": "10.00",
        "limit_down": "8.00",
        "at_limit_up": False,
        "at_limit_down": False,
        "one_price_limit_up": False,
        "one_price_limit_down": False,
        "can_buy": True,
        "can_sell": True,
        "block_reasons": [],
        "schema_version": "m2-tradeability-v1",
    }


def _lineage(symbol: str, accepted_close: str, cash_per_ten: str, derived_close: str) -> dict[str, object]:
    return {
        "schema_version": "m2-daily-lineage-v1",
        "symbol": symbol,
        "target_session": BUSINESS_DATE.isoformat(),
        "kind": "cash_dividend_reference",
        "source": "tencent_archive",
        "details": {
            "previous_session": "2026-07-30",
            "registration_date": "2026-07-30",
            "ex_rights_date": BUSINESS_DATE.isoformat(),
            "accepted_previous_close": accepted_close,
            "cash_per_ten_shares": cash_per_ten,
            "factor_reference_close": format(Decimal(accepted_close) - Decimal(cash_per_ten) / 10, "f"),
            "derived_previous_close": derived_close,
            "action_content": f"10派{cash_per_ten}元",
            "vendor_action_sha256": "a" * 64,
        },
    }


def _evidence() -> SimpleNamespace:
    primary = [_bar("601727", "6.88"), _bar("601866", "2.46")]
    facts = [_fact("601727"), _fact("601866")]
    lineage = [
        _lineage("601727", "6.8900", "0.1425", "6.88"),
        _lineage("601866", "2.4700", "0.1500", "2.46"),
    ]
    manifest = {
        "manifest_version": "m2-daily-incremental-manifest-v6",
        "schema_version": "m2-daily-incremental-v6",
        "dataset_id": RELEASE_ID,
        "base_history_dataset_id": "m2-full-fixed-history",
        "authoritative": False,
        "simulation_orders_allowed": False,
        "accepted": True,
        "target_session": BUSINESS_DATE.isoformat(),
        "scope_sha256": SCOPE,
        "expected_symbol_count": 2,
        "primary_row_count": len(primary),
        "tradeability_row_count": len(facts),
        "lineage_evidence_count": len(lineage),
        "primary_sha256": sha256(primary),
        "tradeability_sha256": sha256(facts),
        "lineage_evidence_sha256": sha256(lineage),
        "gates": [{"name": "fixture", "critical": True, "passed": True}],
    }
    return SimpleNamespace(
        manifest=manifest,
        primary_bars=primary,
        tradeability=facts,
        verification_bars=[],
        adjusted_bars=[{"qfq_factor": "999"}],
        adjustments=[],
        lineage_evidence=lineage,
    )


def _policy() -> M2AdmissionPolicy:
    return M2AdmissionPolicy(frozenset({RELEASE_ID}), 2)


class M2ReleaseAdmissionTests(unittest.TestCase):
    def test_admits_explicit_raw_research_release_and_ignores_vendor_factors(self) -> None:
        evidence = _evidence()
        release = admit_daily_evidence(evidence, _policy())
        self.assertEqual(release.release_id, RELEASE_ID)
        self.assertEqual(release.symbols, frozenset({"601727", "601866"}))
        self.assertEqual([row.cash_before_tax for row in release.cash_dividends], [Decimal("0.1425"), Decimal("0.1500")])
        self.assertNotIn("adjusted_bars", release.__dataclass_fields__)

    def test_rejects_unpinned_release_even_if_marked_accepted(self) -> None:
        with self.assertRaisesRegex(ValueError, "not explicitly admitted"):
            admit_daily_evidence(_evidence(), M2AdmissionPolicy(frozenset({"another-release"}), 2))

    def test_rejects_tampered_raw_hash_and_research_boundary_escape(self) -> None:
        evidence = _evidence()
        evidence.primary_bars[0]["close"] = "7.00"
        with self.assertRaisesRegex(ValueError, "does not reconcile"):
            admit_daily_evidence(evidence, _policy())

        evidence = _evidence()
        evidence.manifest["simulation_orders_allowed"] = True
        with self.assertRaisesRegex(ValueError, "research-only boundary"):
            admit_daily_evidence(evidence, _policy())

    def test_missing_bar_must_be_an_explicit_trade_block(self) -> None:
        evidence = _evidence()
        evidence.primary_bars.pop()
        evidence.manifest["primary_row_count"] = 1
        evidence.manifest["primary_sha256"] = sha256(evidence.primary_bars)
        with self.assertRaisesRegex(ValueError, "raw-bar/tradeability mismatch"):
            admit_daily_evidence(evidence, _policy())
if __name__ == "__main__":
    unittest.main()

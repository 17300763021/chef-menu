from __future__ import annotations

import unittest
from copy import deepcopy

from scripts.simulation.m2_history_source import (
    ACCEPTANCE_SESSIONS,
    ACCEPTANCE_SYMBOLS,
    M2BoundedResearchInput,
    PINNED_DAILY_RELEASE_IDS,
    PINNED_HISTORY_DATASET_ID,
)


def bounded_mapping() -> dict:
    bars = []
    facts = []
    prices = {
        "600519": [(1410, 1412), (1412, 1408), (1408, 1415)],
        "601866": [(2.50, 2.52), (2.52, 2.48), (2.48, 2.51)],
    }
    for session_index, session in enumerate(ACCEPTANCE_SESSIONS):
        for symbol in ACCEPTANCE_SYMBOLS:
            previous, close = prices[symbol][session_index]
            bars.append({
                "symbol": symbol, "business_date": session.isoformat(),
                "open": str(previous), "high": str(max(previous, close) + 1),
                "low": str(min(previous, close) - 1 if symbol == "600519" else min(previous, close) - 0.01),
                "close": str(close), "previous_close": str(previous),
                "previous_close_origin": "stored_m2_raw",
                "volume": 1000000, "total_turnover": str(close * 1000000), "adjustment": "none",
                "source_bar_sha256": "d" * 64, "source_tradeability_sha256": "e" * 64,
            })
            actionable = symbol == "600519" and session == ACCEPTANCE_SESSIONS[1]
            facts.append({
                "symbol": symbol, "business_date": session.isoformat(), "has_primary_bar": True,
                "is_suspended": False, "is_st": False,
                "limit_up": str(previous * 1.1), "limit_down": str(previous * 0.9),
                "price_limit_origin": "stored_m2_fact",
                "can_buy": actionable, "can_sell": actionable,
                "at_limit_up": False, "at_limit_down": False,
                "one_price_limit_up": False, "one_price_limit_down": False,
            })
    return {
        "schema_version": "m3-rqalpha-bounded-input-v1",
        "history_dataset_id": PINNED_HISTORY_DATASET_ID,
        "daily_release_ids": list(PINNED_DAILY_RELEASE_IDS),
        "sessions": [value.isoformat() for value in ACCEPTANCE_SESSIONS],
        "symbols": list(ACCEPTANCE_SYMBOLS),
        "instruments": [
            {"symbol": "600519", "exchange": "SSE", "name": "贵州茅台", "ipo_date": "2001-08-27"},
            {"symbol": "601866", "exchange": "SSE", "name": "中远海发", "ipo_date": "2007-12-12"},
        ],
        "bars": bars, "tradeability": facts,
        "source_manifest_sha256s": ["a" * 64, "b" * 64, "c" * 64],
        "authoritative": False, "simulation_orders_allowed": False,
    }


class M2BoundedHistoryAdapterTests(unittest.TestCase):
    def test_accepts_only_exact_raw_research_scope(self) -> None:
        value = M2BoundedResearchInput.from_mapping(bounded_mapping())
        self.assertEqual(len(value.bars), 6)
        self.assertEqual(len(value.tradeability), 6)
        self.assertFalse(value.authoritative)
        self.assertFalse(value.simulation_orders_allowed)
        self.assertEqual(len(value.input_sha256), 64)

    def test_rejects_unpinned_release_scope_or_vendor_adjustment(self) -> None:
        row = bounded_mapping()
        row["history_dataset_id"] = "latest"
        with self.assertRaisesRegex(ValueError, "not explicitly pinned"):
            M2BoundedResearchInput.from_mapping(row)

        row = bounded_mapping()
        row["bars"][0]["adjustment"] = "qfq"
        with self.assertRaisesRegex(ValueError, "raw unadjusted"):
            M2BoundedResearchInput.from_mapping(row)

        row = bounded_mapping()
        row["simulation_orders_allowed"] = True
        with self.assertRaisesRegex(ValueError, "research-only"):
            M2BoundedResearchInput.from_mapping(row)

    def test_missing_or_suspended_data_fails_closed(self) -> None:
        row = bounded_mapping()
        row["bars"].pop()
        with self.assertRaisesRegex(ValueError, "do not reconcile"):
            M2BoundedResearchInput.from_mapping(row)

        row = bounded_mapping()
        row["tradeability"][2]["is_suspended"] = True
        with self.assertRaisesRegex(ValueError, "fail closed"):
            M2BoundedResearchInput.from_mapping(row)

    def test_declared_input_hash_detects_tampering(self) -> None:
        original = M2BoundedResearchInput.from_mapping(bounded_mapping())
        row = original.to_mapping()
        tampered = deepcopy(row)
        tampered["bars"][0]["close"] = "999"
        with self.assertRaisesRegex(ValueError, "hash does not reconcile|OHLC"):
            M2BoundedResearchInput.from_mapping(tampered)


if __name__ == "__main__":
    unittest.main()

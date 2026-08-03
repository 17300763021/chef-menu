from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.flow_runner import build_manifest
from scripts.market_data.sample_capture import SAMPLE_SYMBOLS
from scripts.market_data.verified_flow import VerifiedFlowFact
from scripts.market_data.verified_flow import parse_eastmoney_klines


class VerifiedFlowTests(unittest.TestCase):
    def test_exact_date_is_required_and_missing_fields_stay_null(self) -> None:
        payload = {"data": {"klines": [
            "2026-07-30,1,2,3,4,5,6,7,8,9,10,11,12",
            "2026-07-31,100,,,,20,,10,,40,,5,,",
        ]}}
        fact = parse_eastmoney_klines(payload, "000001", date(2026, 7, 31))
        self.assertEqual(str(fact.main_net_inflow_cny), "100")
        self.assertIsNone(fact.small_net_inflow_cny)
        with self.assertRaisesRegex(RuntimeError, "exact flow row"):
            parse_eastmoney_klines(payload, "000001", date(2026, 8, 1))

    def test_latest_row_is_never_substituted(self) -> None:
        payload = {"data": {"klines": ["2026-07-31,1,2,3,4,5,6,7,8,9,10,11,12"]}}
        with self.assertRaises(RuntimeError):
            parse_eastmoney_klines(payload, "000001", date(2026, 7, 30))

    def test_dataset_id_is_content_addressed_and_same_content_is_stable(self) -> None:
        business_date = date(2026, 7, 31)
        facts = [VerifiedFlowFact(
            symbol=SAMPLE_SYMBOLS[0], business_date=business_date,
            main_net_inflow_cny=Decimal("1"), main_net_inflow_ratio=None,
            super_large_net_inflow_cny=None, large_net_inflow_cny=None,
            medium_net_inflow_cny=None, small_net_inflow_cny=None,
        )]
        checkpoints = [
            {"symbol": symbol, "status": "succeeded" if symbol == SAMPLE_SYMBOLS[0] else "unavailable"}
            for symbol in SAMPLE_SYMBOLS
        ]

        first = build_manifest(business_date=business_date, facts=facts, checkpoints=checkpoints)
        replay = build_manifest(business_date=business_date, facts=facts, checkpoints=checkpoints)
        changed = build_manifest(
            business_date=business_date,
            facts=facts,
            checkpoints=[{**row, "status": "succeeded"} for row in checkpoints],
        )

        self.assertEqual(first["dataset_id"], replay["dataset_id"])
        self.assertNotEqual(first["dataset_id"], changed["dataset_id"])


if __name__ == "__main__":
    unittest.main()

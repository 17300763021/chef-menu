from __future__ import annotations

import unittest
from datetime import date

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


if __name__ == "__main__":
    unittest.main()

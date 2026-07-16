from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from scripts.market_data.adjustment_engine import AdjustmentTimeline
from scripts.market_data.historical_contracts import AdjustmentEvent


class AdjustmentEngineTests(unittest.TestCase):
    def test_factor_timeline_changes_only_on_effective_date(self) -> None:
        timeline = AdjustmentTimeline([
            AdjustmentEvent.build("600519", date(2020, 1, 2), "0.8", "2"),
            AdjustmentEvent.build("600519", date(2021, 1, 2), "1", "2.5"),
        ])
        self.assertEqual(timeline.factors_on(date(2019, 12, 31)), (Decimal("1"), Decimal("1")))
        self.assertEqual(timeline.factors_on(date(2020, 1, 2)), (Decimal("0.800000"), Decimal("2.000000")))
        self.assertEqual(timeline.factors_on(date(2022, 1, 1)), (Decimal("1.000000"), Decimal("2.500000")))

    def test_duplicate_effective_date_fails(self) -> None:
        event = AdjustmentEvent.build("600519", date(2020, 1, 2), "1", "1")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            AdjustmentTimeline([event, event])


if __name__ == "__main__":
    unittest.main()

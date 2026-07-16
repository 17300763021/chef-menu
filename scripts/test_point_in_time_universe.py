from __future__ import annotations

import unittest
from datetime import date

from scripts.market_data.pit_universe import HISTORY_START, reconstruct
from scripts.market_data.universe_contracts import CurrentUniverse, IndexChange, UniverseEvent


class PointInTimeUniverseTests(unittest.TestCase):
    def test_reverse_reconstruction_removes_survivorship_bias(self) -> None:
        current = CurrentUniverse(date(2020, 1, 3), {"000300": ("000002",), "000905": ("000003",)}, {}, {})
        event = UniverseEvent(1, "regular", date(2019, 12, 1), date(2020, 1, 2), "fixture", "fixture", "0" * 64, (IndexChange.build("000300", ["000001"], ["000002"]),))
        snapshots = reconstruct(current, [event])
        self.assertEqual(snapshots[HISTORY_START]["000300"], ("000001",))
        self.assertEqual(snapshots[date(2020, 1, 2)]["000300"], ("000002",))

    def test_inconsistent_event_fails_closed(self) -> None:
        current = CurrentUniverse(date(2020, 1, 3), {"000300": ("000001",), "000905": ("000003",)}, {}, {})
        event = UniverseEvent(1, "regular", date(2019, 12, 1), date(2020, 1, 2), "fixture", "fixture", "0" * 64, (IndexChange.build("000300", ["000001"], ["000002"]),))
        with self.assertRaisesRegex(ValueError, "additions absent"):
            reconstruct(current, [event])


if __name__ == "__main__":
    unittest.main()

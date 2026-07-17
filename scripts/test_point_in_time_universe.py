from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.market_data.pit_universe import HISTORY_START, reconstruct
from scripts.market_data.sources.csi_index_source import CsiIndexSource
from scripts.market_data.universe_contracts import CurrentUniverse, IndexChange, UniverseEvent


class PointInTimeUniverseTests(unittest.TestCase):
    @staticmethod
    def retry_source() -> tuple[CsiIndexSource, type[Exception]]:
        class TimeoutErrorForTest(Exception):
            pass

        session = Mock()
        session.headers = {}
        fake_requests = SimpleNamespace(
            Session=Mock(return_value=session),
            Timeout=TimeoutErrorForTest,
            ConnectionError=ConnectionError,
        )
        with patch.dict("sys.modules", {"requests": fake_requests}):
            return CsiIndexSource(timeout_seconds=1, attempts=3, backoff_seconds=0), TimeoutErrorForTest

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

    def test_official_attachment_download_recovers_after_timeout(self) -> None:
        source, timeout_error = self.retry_source()
        response = Mock(content=b"official-evidence")
        response.raise_for_status.return_value = None
        source.session.get = Mock(side_effect=[timeout_error("first"), timeout_error("second"), response])
        self.assertEqual(source._get_bytes("https://official.example/evidence.xlsx"), b"official-evidence")
        self.assertEqual(source.session.get.call_count, 3)

    def test_official_attachment_download_fails_after_bounded_attempts(self) -> None:
        source, timeout_error = self.retry_source()
        source.session.get = Mock(side_effect=timeout_error("still unavailable"))
        with self.assertRaises(timeout_error):
            source._get_bytes("https://official.example/evidence.xlsx")
        self.assertEqual(source.session.get.call_count, 3)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.market_data.manifest import sha256
from scripts.market_data.pit_universe import HISTORY_START, reconstruct
from scripts.market_data.sources.csi_index_source import EXPECTED_EVENT_INDEX_SHA256, CsiIndexSource, load_event_index
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
        with self.assertRaisesRegex(RuntimeError, "CSI official download unavailable after 3 attempts"):
            source._get_bytes("https://official.example/evidence.xlsx")
        self.assertEqual(source.session.get.call_count, 3)

    def test_csi_event_index_is_fixed_and_reproducible(self) -> None:
        events, source = load_event_index()
        self.assertEqual(len(events), 26)
        self.assertEqual(source["accepted_manifest_event_sha256"], EXPECTED_EVENT_INDEX_SHA256)
        self.assertEqual(sha256([event.canonical() for event in events]), EXPECTED_EVENT_INDEX_SHA256)
        self.assertEqual(events[0].notice_id, 11518)
        self.assertEqual(events[-1].notice_id, 3006137)

    def test_csi_event_index_hash_mismatch_fails_closed(self) -> None:
        events, source = load_event_index()
        rows = [event.canonical() for event in events]
        rows[0]["attachment_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            path = f"{temporary}/bad-csi-index.json"
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "schema_version": "m2-csi-pit-event-index-v1",
                    "source": source,
                    "events": rows,
                }, stream)
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_event_index(Path(path))


if __name__ == "__main__":
    unittest.main()

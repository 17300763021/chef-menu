from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.market_data.historical_bars import _write_gzip
from scripts.market_data.manifest import sha256
from scripts.market_data.merge_historical_shards import merge


class MergeHistoricalShardsTests(unittest.TestCase):
    def test_two_complete_shards_merge_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, symbol in enumerate(("000001", "600519")):
                shard = root / "input" / f"shard-{index}"
                shard.mkdir(parents=True)
                bar = {"symbol": symbol, "business_date": "2026-07-15"}
                fact = {"symbol": symbol, "business_date": "2026-07-15"}
                reference = {"symbol": symbol}
                _write_gzip(shard / "historical-bars.json.gz", [bar])
                _write_gzip(shard / "tradeability.json.gz", [fact])
                _write_gzip(shard / "adjustment-events.json.gz", [])
                _write_gzip(shard / "verification-checks.json.gz", [{
                    "symbol": symbol, "business_date": "2026-07-15",
                    "primary_close": "10.00", "verification_close": "10.00",
                }])
                (shard / "security-references.json").write_text(json.dumps([reference]), encoding="utf-8")
                manifest = {
                    "accepted": True, "shard_index": index, "shard_count": 2,
                    "global_symbol_count": 2, "global_expected_key_count": 2,
                    "business_end": "2026-07-15", "mode": "preflight",
                    "expected_key_count": 1, "bar_count": 1,
                    "verification_expected_count": 1, "verification_check_count": 1,
                    "global_verification_symbol_count": 2,
                    "global_verification_symbols_sha256": sha256(["000001", "600519"]),
                    "verification_symbols": [symbol],
                }
                (shard / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = merge(root / "input", root / "output")
            self.assertTrue(result["accepted"])
            self.assertEqual(result["bar_count"], 2)
            self.assertEqual(result["tradeability_count"], 2)
            self.assertTrue(result["merge_checks"]["cross_source_accepted"])

    def test_merge_fails_closed_when_global_verification_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shard = root / "input" / "shard-0"
            shard.mkdir(parents=True)
            _write_gzip(shard / "historical-bars.json.gz", [{"symbol": "000001", "business_date": "2026-07-15"}])
            _write_gzip(shard / "tradeability.json.gz", [{"symbol": "000001", "business_date": "2026-07-15"}])
            _write_gzip(shard / "adjustment-events.json.gz", [])
            _write_gzip(shard / "verification-checks.json.gz", [{
                "symbol": "000001", "business_date": "2026-07-15",
                "primary_close": "10.00", "verification_close": "10.00",
            }])
            (shard / "security-references.json").write_text(json.dumps([{"symbol": "000001"}]), encoding="utf-8")
            (shard / "manifest.json").write_text(json.dumps({
                "accepted": True, "shard_index": 0, "shard_count": 1,
                "global_symbol_count": 1, "global_expected_key_count": 1,
                "business_end": "2026-07-15", "mode": "smoke",
                "expected_key_count": 1, "bar_count": 1,
                "verification_expected_count": 2, "verification_check_count": 1,
                "global_verification_symbol_count": 1,
                "global_verification_symbols_sha256": sha256(["000001"]),
                "verification_symbols": ["000001"],
            }), encoding="utf-8")
            result = merge(root / "input", root / "output")
            self.assertFalse(result["accepted"])
            self.assertFalse(result["merge_checks"]["cross_source_accepted"])


if __name__ == "__main__":
    unittest.main()

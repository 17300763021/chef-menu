from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.market_data.historical_bars import _write_gzip
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
                (shard / "security-references.json").write_text(json.dumps([reference]), encoding="utf-8")
                manifest = {
                    "accepted": True, "shard_index": index, "shard_count": 2,
                    "global_symbol_count": 2, "global_expected_key_count": 2,
                    "business_end": "2026-07-15", "mode": "preflight",
                    "expected_key_count": 1, "bar_count": 1,
                }
                (shard / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = merge(root / "input", root / "output")
            self.assertTrue(result["accepted"])
            self.assertEqual(result["bar_count"], 2)
            self.assertEqual(result["tradeability_count"], 2)


if __name__ == "__main__":
    unittest.main()

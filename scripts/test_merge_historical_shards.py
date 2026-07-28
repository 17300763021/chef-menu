from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from scripts.market_data.historical_bars import _write_gzip
from scripts.market_data.manifest import sha256
from scripts.market_data.merge_historical_shards import merge


SYMBOLS = ("000001", "600519")


def _bar(symbol: str, business_date: date, offset: int) -> dict[str, object]:
    raw_close = "95.00" if offset == 1 else "100.00"
    adjusted_close = "100.00"
    return {
        "symbol": symbol,
        "business_date": business_date.isoformat(),
        "close": raw_close,
        "qfq_close": adjusted_close,
        "hfq_close": adjusted_close,
    }


def _fact(
    symbol: str,
    business_date: date,
    *,
    suspended: bool = False,
    status_known: bool = True,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "business_date": business_date.isoformat(),
        "is_suspended": suspended,
        "has_secondary_status": status_known,
    }


def _write_run(
    root: Path,
    *,
    expected_counts: list[int],
    bar_counts: list[int],
    shard_acceptance: list[bool] | None = None,
    suspended_keys: set[tuple[int, int]] | None = None,
    unknown_status_keys: set[tuple[int, int]] | None = None,
) -> list[Path]:
    shard_count = len(expected_counts)
    if len(bar_counts) != shard_count or shard_count > len(SYMBOLS):
        raise ValueError("invalid test shard configuration")
    acceptance = shard_acceptance or [True] * shard_count
    suspended = suspended_keys or set()
    unknown = unknown_status_keys or set()
    verification_symbols = sorted(SYMBOLS[:shard_count])
    global_expected = sum(expected_counts)
    paths: list[Path] = []
    start = date(2026, 1, 1)

    for index, (expected_count, bar_count) in enumerate(zip(expected_counts, bar_counts, strict=True)):
        symbol = SYMBOLS[index]
        shard = root / "input" / f"shard-{index}"
        shard.mkdir(parents=True)
        paths.append(shard)
        days = [start + timedelta(days=offset) for offset in range(expected_count)]
        bars = [_bar(symbol, day, offset) for offset, day in enumerate(days[:bar_count])]
        facts = [
            _fact(
                symbol,
                day,
                suspended=(index, offset) in suspended,
                status_known=(index, offset) not in unknown,
            )
            for offset, day in enumerate(days)
        ]
        adjustments = []
        if index == 0 and bar_count >= 2:
            adjustments.append({
                "symbol": symbol,
                "effective_date": days[1].isoformat(),
                "qfq_factor": "0.950000",
                "hfq_factor": "1.050000",
            })
        verification_checks = [{
            "symbol": symbol,
            "business_date": days[0].isoformat(),
            "primary_close": "100.00",
            "verification_close": "100.00",
        }]

        _write_gzip(shard / "historical-bars.json.gz", bars)
        _write_gzip(shard / "tradeability.json.gz", facts)
        _write_gzip(shard / "adjustment-events.json.gz", adjustments)
        _write_gzip(shard / "verification-checks.json.gz", verification_checks)
        (shard / "security-references.json").write_text(
            json.dumps([{"symbol": symbol}]),
            encoding="utf-8",
        )
        manifest = {
            "manifest_version": "m2-historical-market-manifest-v1",
            "authoritative": False,
            "simulation_orders_allowed": False,
            "accepted": acceptance[index],
            "shard_index": index,
            "shard_count": shard_count,
            "global_symbol_count": shard_count,
            "global_expected_key_count": global_expected,
            "business_end": "2026-07-15",
            "mode": "preflight",
            "expected_key_count": expected_count,
            "bar_count": bar_count,
            "tradeability_count": expected_count,
            "adjustment_event_count": len(adjustments),
            "reference_count": 1,
            "verification_expected_count": 1,
            "verification_check_count": 1,
            "checkpoint_dataset_id": f"test-shard-{index}",
            "bars_sha256": sha256(bars),
            "tradeability_sha256": sha256(facts),
            "adjustments_sha256": sha256(adjustments),
            "references_sha256": sha256([{"symbol": symbol}]),
            "verification_checks_sha256": sha256(verification_checks),
            "global_verification_symbol_count": shard_count,
            "global_verification_symbols_sha256": sha256(verification_symbols),
            "verification_symbols": [symbol],
        }
        (shard / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return paths


class MergeHistoricalShardsTests(unittest.TestCase):
    def test_two_complete_shards_merge_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_run(root, expected_counts=[2, 2], bar_counts=[2, 2])

            result = merge(root / "input", root / "output")

            self.assertTrue(result["accepted"])
            self.assertEqual(result["bar_count"], 4)
            self.assertEqual(result["tradeability_count"], 4)
            self.assertTrue(result["merge_checks"]["global_aggregate_accepted"])
            self.assertTrue(result["merge_checks"]["cross_source_accepted"])

    def test_merge_fails_closed_when_global_verification_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shards = _write_run(root, expected_counts=[2], bar_counts=[2])
            manifest_path = shards[0] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["verification_expected_count"] = 2
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = merge(root / "input", root / "output")

            self.assertFalse(result["accepted"])
            self.assertFalse(result["merge_checks"]["cross_source_accepted"])

    def test_merge_uses_global_coverage_instead_of_arbitrary_shard_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_run(
                root,
                expected_counts=[10, 90],
                bar_counts=[9, 90],
                unknown_status_keys={(0, 9)},
            )

            result = merge(root / "input", root / "output")

            coverage = next(
                gate for gate in result["global_aggregate_gates"]
                if gate["name"] == "historical_active_coverage"
            )
            self.assertTrue(result["accepted"])
            self.assertEqual(coverage["actual"], "99/100 (99.00%)")
            self.assertEqual(result["unknown_status_count"], 1)
            self.assertEqual(result["missing_active_bar_count"], 1)

    def test_merge_fails_closed_when_global_coverage_is_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_run(root, expected_counts=[10, 90], bar_counts=[7, 90])

            result = merge(root / "input", root / "output")

            self.assertFalse(result["accepted"])
            self.assertFalse(result["merge_checks"]["global_aggregate_accepted"])

    def test_merge_excludes_only_confirmed_suspensions_from_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_run(
                root,
                expected_counts=[10, 90],
                bar_counts=[9, 90],
                suspended_keys={(0, 9)},
            )

            result = merge(root / "input", root / "output")

            coverage = next(
                gate for gate in result["global_aggregate_gates"]
                if gate["name"] == "historical_active_coverage"
            )
            self.assertTrue(result["accepted"])
            self.assertEqual(coverage["actual"], "99/99 (100.00%)")
            self.assertEqual(result["confirmed_suspension_count"], 1)
            self.assertEqual(result["missing_active_bar_count"], 0)

    def test_merge_preserves_other_shard_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_run(
                root,
                expected_counts=[2, 2],
                bar_counts=[2, 2],
                shard_acceptance=[False, True],
            )

            result = merge(root / "input", root / "output")

            self.assertFalse(result["accepted"])
            self.assertFalse(result["merge_checks"]["all_shards_accepted"])

    def test_merge_fails_closed_when_a_shard_file_does_not_match_its_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shards = _write_run(root, expected_counts=[2], bar_counts=[2])
            _write_gzip(shards[0] / "historical-bars.json.gz", [])

            result = merge(root / "input", root / "output")

            self.assertFalse(result["accepted"])
            self.assertFalse(result["merge_checks"]["shard_content_hashes_reconcile"])


if __name__ == "__main__":
    unittest.main()

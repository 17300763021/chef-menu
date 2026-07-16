"""Merge independently accepted M2.3 shards into one deterministic evidence set."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any

from scripts.market_data.historical_bars import _write_gzip
from scripts.market_data.manifest import sha256


def _read_json(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def merge(input_dir: Path, output_dir: Path) -> dict[str, Any]:
    manifests = [_read_json(path) for path in sorted(input_dir.rglob("manifest.json"))]
    if not manifests:
        raise RuntimeError("no shard manifests found")
    shard_count = manifests[0]["shard_count"]
    indices = [manifest["shard_index"] for manifest in manifests]
    consistent = all(
        manifest["shard_count"] == shard_count
        and manifest["global_symbol_count"] == manifests[0]["global_symbol_count"]
        and manifest["global_expected_key_count"] == manifests[0]["global_expected_key_count"]
        and manifest["business_end"] == manifests[0]["business_end"]
        and manifest["mode"] == manifests[0]["mode"]
        for manifest in manifests
    )
    inventory_ok = sorted(indices) == list(range(shard_count))
    all_shards_accepted = all(manifest["accepted"] for manifest in manifests)

    bars: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    adjustments: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    for manifest_path in sorted(input_dir.rglob("manifest.json")):
        parent = manifest_path.parent
        bars.extend(_read_json(parent / "historical-bars.json.gz"))
        facts.extend(_read_json(parent / "tradeability.json.gz"))
        adjustments.extend(_read_json(parent / "adjustment-events.json.gz"))
        references.extend(_read_json(parent / "security-references.json"))

    bars.sort(key=lambda row: (row["symbol"], row["business_date"]))
    facts.sort(key=lambda row: (row["symbol"], row["business_date"]))
    adjustments.sort(key=lambda row: (row["symbol"], row["effective_date"]))
    references.sort(key=lambda row: row["symbol"])
    bar_keys = [(row["symbol"], row["business_date"]) for row in bars]
    fact_keys = [(row["symbol"], row["business_date"]) for row in facts]
    reference_keys = [row["symbol"] for row in references]
    no_duplicates = len(bar_keys) == len(set(bar_keys)) and len(fact_keys) == len(set(fact_keys)) and len(reference_keys) == len(set(reference_keys))
    expected_reconciles = (
        sum(manifest["expected_key_count"] for manifest in manifests) == manifests[0]["global_expected_key_count"]
        and len(facts) == manifests[0]["global_expected_key_count"]
        and sum(manifest["bar_count"] for manifest in manifests) == len(bars)
    )
    accepted = consistent and inventory_ok and all_shards_accepted and no_duplicates and expected_reconciles
    result = {
        "manifest_version": "m2-historical-market-merged-manifest-v1",
        "authoritative": False, "simulation_orders_allowed": False,
        "mode": manifests[0]["mode"], "business_end": manifests[0]["business_end"],
        "shard_count": shard_count, "global_symbol_count": manifests[0]["global_symbol_count"],
        "global_expected_key_count": manifests[0]["global_expected_key_count"],
        "bar_count": len(bars), "tradeability_count": len(facts),
        "adjustment_event_count": len(adjustments), "reference_count": len(references),
        "bars_sha256": sha256(bars), "tradeability_sha256": sha256(facts),
        "adjustments_sha256": sha256(adjustments), "references_sha256": sha256(references),
        "accepted": accepted,
        "merge_checks": {
            "consistent_shard_metadata": consistent, "complete_shard_inventory": inventory_ok,
            "all_shards_accepted": all_shards_accepted, "no_cross_shard_duplicates": no_duplicates,
            "expected_counts_reconcile": expected_reconciles,
        },
        "shard_manifests": manifests,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_gzip(output_dir / "historical-bars.json.gz", bars)
    _write_gzip(output_dir / "tradeability.json.gz", facts)
    _write_gzip(output_dir / "adjustment-events.json.gz", adjustments)
    (output_dir / "security-references.json").write_text(json.dumps(references, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge M2.3 historical shards")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = merge(args.input_dir, args.output_dir)
    print(json.dumps({key: result[key] for key in ("accepted", "mode", "shard_count", "global_symbol_count", "global_expected_key_count", "bar_count", "tradeability_count", "bars_sha256", "tradeability_sha256", "merge_checks")}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    sys.exit(main())

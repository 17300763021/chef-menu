"""Merge independently accepted M2.3 shards into one deterministic evidence set."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from scripts.market_data.historical_bars import _write_gzip
from scripts.market_data.historical_quality_gates import (
    evaluate_active_coverage,
    evaluate_adjustment_alignment,
    evaluate_cross_source,
)
from scripts.market_data.manifest import sha256
from scripts.market_data.quality_gates import accepted as gates_accepted


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
        and manifest.get("global_verification_symbol_count") == manifests[0].get("global_verification_symbol_count")
        and manifest.get("global_verification_symbols_sha256") == manifests[0].get("global_verification_symbols_sha256")
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
    verification_checks: list[dict[str, Any]] = []
    shard_content_hashes_reconcile = True
    for manifest_path in sorted(input_dir.rglob("manifest.json")):
        parent = manifest_path.parent
        shard_manifest = _read_json(manifest_path)
        shard_bars = _read_json(parent / "historical-bars.json.gz")
        shard_facts = _read_json(parent / "tradeability.json.gz")
        shard_adjustments = _read_json(parent / "adjustment-events.json.gz")
        shard_references = _read_json(parent / "security-references.json")
        shard_verifications = _read_json(parent / "verification-checks.json.gz")
        shard_content_hashes_reconcile = shard_content_hashes_reconcile and all((
            shard_manifest.get("bar_count") == len(shard_bars),
            shard_manifest.get("tradeability_count") == len(shard_facts),
            shard_manifest.get("adjustment_event_count") == len(shard_adjustments),
            shard_manifest.get("reference_count") == len(shard_references),
            shard_manifest.get("verification_check_count") == len(shard_verifications),
            shard_manifest.get("bars_sha256") == sha256(shard_bars),
            shard_manifest.get("tradeability_sha256") == sha256(shard_facts),
            shard_manifest.get("adjustments_sha256") == sha256(shard_adjustments),
            shard_manifest.get("references_sha256") == sha256(shard_references),
            shard_manifest.get("verification_checks_sha256") == sha256(shard_verifications),
        ))
        bars.extend(shard_bars)
        facts.extend(shard_facts)
        adjustments.extend(shard_adjustments)
        references.extend(shard_references)
        verification_checks.extend(shard_verifications)

    bars.sort(key=lambda row: (row["symbol"], row["business_date"]))
    facts.sort(key=lambda row: (row["symbol"], row["business_date"]))
    adjustments.sort(key=lambda row: (row["symbol"], row["effective_date"]))
    references.sort(key=lambda row: row["symbol"])
    verification_checks.sort(key=lambda row: (row["symbol"], row["business_date"]))
    bar_keys = [(row["symbol"], row["business_date"]) for row in bars]
    fact_keys = [(row["symbol"], row["business_date"]) for row in facts]
    reference_keys = [row["symbol"] for row in references]
    verification_keys = [(row["symbol"], row["business_date"]) for row in verification_checks]
    no_duplicates = (
        len(bar_keys) == len(set(bar_keys))
        and len(fact_keys) == len(set(fact_keys))
        and len(reference_keys) == len(set(reference_keys))
        and len(verification_keys) == len(set(verification_keys))
    )
    expected_reconciles = (
        sum(manifest["expected_key_count"] for manifest in manifests) == manifests[0]["global_expected_key_count"]
        and len(facts) == manifests[0]["global_expected_key_count"]
        and sum(manifest["bar_count"] for manifest in manifests) == len(bars)
    )
    verification_expected = sum(manifest.get("verification_expected_count", 0) for manifest in manifests)
    verification_reconciles = sum(manifest.get("verification_check_count", 0) for manifest in manifests) == len(verification_checks)
    verification_symbols = sorted(symbol for manifest in manifests for symbol in manifest.get("verification_symbols", []))
    verification_inventory_ok = (
        len(verification_symbols) == manifests[0].get("global_verification_symbol_count", 0)
        and len(verification_symbols) == len(set(verification_symbols))
        and sha256(verification_symbols) == manifests[0].get("global_verification_symbols_sha256")
    )
    expected_scope = {
        (row["symbol"], date.fromisoformat(row["business_date"]))
        for row in facts
    }
    observed_scope = {
        (row["symbol"], date.fromisoformat(row["business_date"]))
        for row in bars
    }
    suspended_scope = {
        (row["symbol"], date.fromisoformat(row["business_date"]))
        for row in facts
        if row.get("is_suspended", False)
    }
    aggregate_gates = [
        evaluate_active_coverage(expected_scope, observed_scope, suspended_scope),
        evaluate_adjustment_alignment(
            (
                (
                    row["symbol"],
                    date.fromisoformat(row["business_date"]),
                    Decimal(row["close"]),
                    Decimal(row["qfq_close"]),
                    Decimal(row["hfq_close"]),
                )
                for row in bars
            ),
            (
                (
                    row["symbol"],
                    date.fromisoformat(row["effective_date"]),
                    Decimal(row["qfq_factor"]),
                    Decimal(row["hfq_factor"]),
                )
                for row in adjustments
            ),
        ),
    ]
    aggregate_accepted = gates_accepted(aggregate_gates)
    cross_source_gates = evaluate_cross_source(
        [
            (
                row["symbol"], date.fromisoformat(row["business_date"]),
                Decimal(row["primary_close"]), Decimal(row["verification_close"]),
            )
            for row in verification_checks
        ],
        verification_expected,
    )
    cross_source_accepted = verification_reconciles and verification_inventory_ok and gates_accepted(cross_source_gates)
    accepted = (
        consistent
        and inventory_ok
        and all_shards_accepted
        and shard_content_hashes_reconcile
        and no_duplicates
        and expected_reconciles
        and aggregate_accepted
        and cross_source_accepted
    )
    result = {
        "manifest_version": "m2-historical-market-merged-manifest-v2",
        "authoritative": False, "simulation_orders_allowed": False,
        "mode": manifests[0]["mode"], "business_end": manifests[0]["business_end"],
        "shard_count": shard_count, "global_symbol_count": manifests[0]["global_symbol_count"],
        "global_expected_key_count": manifests[0]["global_expected_key_count"],
        "bar_count": len(bars), "tradeability_count": len(facts),
        "adjustment_event_count": len(adjustments), "reference_count": len(references),
        "confirmed_suspension_count": len(suspended_scope),
        "unknown_status_count": sum(not row.get("has_secondary_status", False) for row in facts),
        "missing_active_bar_count": len((expected_scope - suspended_scope) - observed_scope),
        "verification_expected_count": verification_expected,
        "verification_check_count": len(verification_checks),
        "bars_sha256": sha256(bars), "tradeability_sha256": sha256(facts),
        "adjustments_sha256": sha256(adjustments), "references_sha256": sha256(references),
        "verification_checks_sha256": sha256(verification_checks),
        "accepted": accepted,
        "merge_checks": {
            "consistent_shard_metadata": consistent, "complete_shard_inventory": inventory_ok,
            "all_shards_accepted": all_shards_accepted, "no_cross_shard_duplicates": no_duplicates,
            "shard_content_hashes_reconcile": shard_content_hashes_reconcile,
            "expected_counts_reconcile": expected_reconciles,
            "global_aggregate_accepted": aggregate_accepted,
            "verification_counts_reconcile": verification_reconciles,
            "verification_symbol_inventory_reconciles": verification_inventory_ok,
            "cross_source_accepted": cross_source_accepted,
        },
        "global_aggregate_gates": [gate.canonical() for gate in aggregate_gates],
        "cross_source_gates": [gate.canonical() for gate in cross_source_gates],
        "shard_manifests": manifests,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_gzip(output_dir / "historical-bars.json.gz", bars)
    _write_gzip(output_dir / "tradeability.json.gz", facts)
    _write_gzip(output_dir / "adjustment-events.json.gz", adjustments)
    _write_gzip(output_dir / "verification-checks.json.gz", verification_checks)
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

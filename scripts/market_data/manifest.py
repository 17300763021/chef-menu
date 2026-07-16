"""Deterministic content manifest for non-authoritative M2.1 evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Iterable

from scripts.market_data.contracts import DailyBar, SCHEMA_VERSION, canonical_rows
from scripts.market_data.quality_gates import GateResult, accepted


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def build_manifest(rows: Iterable[DailyBar], gates: Iterable[GateResult], sample: dict[str, Any]) -> dict[str, Any]:
    normalized = canonical_rows(rows)
    gate_rows = [gate.canonical() for gate in gates]
    source_counts = Counter(row["source"] for row in normalized)
    dates = sorted({row["business_date"] for row in normalized})
    return {
        "manifest_version": "m2-source-admission-manifest-v1",
        "schema_version": SCHEMA_VERSION,
        "authoritative": False,
        "simulation_orders_allowed": False,
        "sample": sample,
        "row_count": len(normalized),
        "source_counts": dict(sorted(source_counts.items())),
        "minimum_business_date": dates[0] if dates else None,
        "maximum_business_date": dates[-1] if dates else None,
        "dataset_sha256": sha256(normalized),
        "quality_sha256": sha256(gate_rows),
        "accepted": accepted(gates),
        "gates": gate_rows,
    }

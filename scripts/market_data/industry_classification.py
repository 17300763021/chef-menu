"""Deterministic reconstruction and quality gates for M2.5 industry data."""

from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scripts.market_data.industry_contracts import (
    INDUSTRY_SCHEMA_VERSION,
    SW_2021_EFFECTIVE_DATE,
    IndustryInterval,
    IndustryNode,
    IndustryScopeSecurity,
    IndustryVerification,
    SwsAssignmentRecord,
    classification_version,
)
from scripts.market_data.manifest import sha256
from scripts.market_data.quality_gates import GateResult, accepted


HISTORY_START = date(2018, 1, 1)
MANIFEST_VERSION = "m2-industry-pit-manifest-v1"


def canonical_scope(scope: Iterable[IndustryScopeSecurity]) -> list[dict[str, Any]]:
    return [item.canonical() for item in sorted(scope, key=lambda value: value.symbol)]


def build_intervals(
    scope: Sequence[IndustryScopeSecurity],
    source_rows: Sequence[SwsAssignmentRecord],
    *,
    observed_on: date,
    as_of_date: date,
    history_start: date = HISTORY_START,
) -> list[IndustryInterval]:
    if observed_on < as_of_date:
        raise ValueError("industry observation date cannot precede the dataset as-of date")
    by_symbol: dict[str, list[SwsAssignmentRecord]] = defaultdict(list)
    for row in source_rows:
        by_symbol[row.symbol].append(row)
    intervals: list[IndustryInterval] = []
    for security in sorted(scope, key=lambda value: value.symbol):
        timeline_start = max(history_start, security.ipo_date)
        timeline_end = min(
            as_of_date + timedelta(days=1),
            (security.out_date + timedelta(days=1)) if security.out_date is not None else as_of_date + timedelta(days=1),
        )
        if timeline_end <= timeline_start:
            continue
        rows = sorted(by_symbol.get(security.symbol, []), key=lambda value: value.source_effective_from)
        for index, row in enumerate(rows):
            next_start = rows[index + 1].source_effective_from if index + 1 < len(rows) else timeline_end
            valid_from = max(timeline_start, row.source_effective_from)
            valid_to = min(timeline_end, next_start)
            if valid_to <= valid_from:
                continue
            intervals.append(IndustryInterval.build(
                symbol=security.symbol,
                source_effective_from=row.source_effective_from,
                valid_from=valid_from,
                valid_to=valid_to,
                industry_code=row.industry_code,
                observed_on=observed_on,
                source_updated_at=row.source_updated_at,
            ))
    return sorted(intervals, key=lambda value: (value.symbol, value.valid_from))


def _node_name_map(nodes: Iterable[IndustryNode]) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in nodes:
        code = node.node_code.upper()
        if code.startswith("S"):
            code = code[1:]
        if code.isdigit() and len(code) in {2, 4, 6} and node.termination_date is None:
            result[code] = node.node_name
    return result


def _consensus(values: Iterable[str | None]) -> str | None:
    observed = {value.strip() for value in values if value and value.strip()}
    return next(iter(observed)) if len(observed) == 1 else None


def enrich_interval_names(
    intervals: Sequence[IndustryInterval],
    verifications: Sequence[IndustryVerification],
    nodes: Sequence[IndustryNode],
) -> list[IndustryInterval]:
    exact: dict[tuple[str, date, str], list[IndustryVerification]] = defaultdict(list)
    by_symbol_code: dict[tuple[str, str], list[IndustryVerification]] = defaultdict(list)
    for row in verifications:
        exact[(row.symbol, row.change_date, row.industry_code)].append(row)
        by_symbol_code[(row.symbol, row.industry_code)].append(row)
    node_names = _node_name_map(nodes)
    result: list[IndustryInterval] = []
    for interval in intervals:
        candidates = exact.get((interval.symbol, interval.source_effective_from, interval.industry_code), [])
        if not candidates:
            candidates = [
                row for row in by_symbol_code.get((interval.symbol, interval.industry_code), [])
                if classification_version(row.change_date) == interval.classification_version
            ]
        allow_current_node_fallback = interval.classification_version == "sw_2021"
        l1 = _consensus(row.level1_name for row in candidates)
        l2 = _consensus(row.level2_name for row in candidates)
        l3 = _consensus(row.level3_name for row in candidates)
        if allow_current_node_fallback:
            l1 = l1 or node_names.get(interval.level1_code)
            l2 = l2 or node_names.get(interval.level2_code)
            l3 = l3 or node_names.get(interval.level3_code)
        result.append(interval.with_names(l1, l2, l3))
    return result


def _timeline_bounds(
    security: IndustryScopeSecurity,
    history_start: date,
    as_of_date: date,
) -> tuple[date, date] | None:
    start = max(history_start, security.ipo_date)
    end = min(
        as_of_date + timedelta(days=1),
        (security.out_date + timedelta(days=1)) if security.out_date is not None else as_of_date + timedelta(days=1),
    )
    return None if end <= start else (start, end)


def evaluate_industry(
    *,
    scope: Sequence[IndustryScopeSecurity],
    source_rows: Sequence[SwsAssignmentRecord],
    intervals: Sequence[IndustryInterval],
    verifications: Sequence[IndustryVerification],
    nodes: Sequence[IndustryNode],
    history_start: date,
    as_of_date: date,
    expected_scope_count: int,
) -> list[GateResult]:
    results: list[GateResult] = []
    scope_symbols = {item.symbol for item in scope}
    results.append(GateResult(
        "frozen_scope_inventory",
        len(scope_symbols) == expected_scope_count == len(scope),
        len(scope_symbols),
        f"= {expected_scope_count}",
    ))

    unexpected_symbols = sorted(
        ({row.symbol for row in source_rows}
         | {row.symbol for row in intervals}
         | {row.symbol for row in verifications})
        - scope_symbols
    )
    results.append(GateResult(
        "evidence_scope_membership",
        not unexpected_symbols,
        len(unexpected_symbols),
        "= 0 symbols outside the frozen scope",
        details=tuple(unexpected_symbols[:50]),
    ))

    source_symbols = {row.symbol for row in source_rows}
    source_coverage_bps = len(scope_symbols & source_symbols) * 10000 // max(1, len(scope_symbols))
    results.append(GateResult(
        "official_assignment_symbol_coverage",
        source_coverage_bps >= 9800,
        f"{source_coverage_bps / 100:.2f}%",
        ">= 98.00%",
        details=tuple(sorted(scope_symbols - source_symbols)[:50]),
    ))

    keys = [row.key for row in intervals]
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    results.append(GateResult(
        "interval_duplicate_keys", not duplicates, len(duplicates), "= 0",
        details=tuple(f"{symbol}:{day.isoformat()}" for symbol, day in duplicates[:50]),
    ))

    by_symbol: dict[str, list[IndustryInterval]] = defaultdict(list)
    for row in intervals:
        by_symbol[row.symbol].append(row)
    complete: set[str] = set()
    overlaps: list[str] = []
    gaps: list[str] = []
    for security in scope:
        bounds = _timeline_bounds(security, history_start, as_of_date)
        if bounds is None:
            continue
        start, end = bounds
        rows = sorted(by_symbol.get(security.symbol, []), key=lambda value: value.valid_from)
        cursor = start
        for row in rows:
            if row.valid_from < cursor:
                overlaps.append(f"{security.symbol}:{row.valid_from.isoformat()}<{cursor.isoformat()}")
            elif row.valid_from > cursor:
                gaps.append(f"{security.symbol}:{cursor.isoformat()}..{row.valid_from.isoformat()}")
            cursor = max(cursor, row.valid_to)
        if cursor < end:
            gaps.append(f"{security.symbol}:{cursor.isoformat()}..{end.isoformat()}")
        if rows and rows[0].valid_from == start and cursor == end and not any(
            value.startswith(f"{security.symbol}:") for value in (*overlaps, *gaps)
        ):
            complete.add(security.symbol)
    results.append(GateResult(
        "interval_overlaps", not overlaps, len(overlaps), "= 0", details=tuple(overlaps[:50]),
    ))
    complete_bps = len(complete) * 10000 // max(1, len(scope_symbols))
    results.append(GateResult(
        "point_in_time_interval_coverage",
        complete_bps >= 9800,
        f"{complete_bps / 100:.2f}%",
        ">= 98.00%",
        details=tuple(gaps[:50]),
    ))

    invalid_codes = [f"{row.symbol}:{row.industry_code}" for row in intervals if len(row.industry_code) != 6 or not row.industry_code.isdigit()]
    results.append(GateResult(
        "industry_code_contract", not invalid_codes, len(invalid_codes), "= 0", details=tuple(invalid_codes[:50]),
    ))

    active_on_cutover = {
        item.symbol for item in scope
        if item.ipo_date <= SW_2021_EFFECTIVE_DATE
        and (item.out_date is None or item.out_date >= SW_2021_EFFECTIVE_DATE)
        and SW_2021_EFFECTIVE_DATE <= as_of_date
    }
    cutover_rows = {
        row.symbol for row in source_rows if row.source_effective_from == SW_2021_EFFECTIVE_DATE
    }
    missing_cutover = sorted(active_on_cutover - cutover_rows)
    results.append(GateResult(
        "classification_version_cutover",
        not missing_cutover,
        len(missing_cutover),
        "= 0 active securities without a 2021-07-30 classification",
        details=tuple(missing_cutover[:50]),
    ))

    latest_interval = {
        symbol: max(rows, key=lambda value: value.valid_to)
        for symbol, rows in by_symbol.items() if rows
    }
    named = {symbol for symbol, row in latest_interval.items() if row.level1_name}
    name_coverage_bps = len(named) * 10000 // max(1, len(scope_symbols))
    results.append(GateResult(
        "level1_name_coverage",
        name_coverage_bps >= 9800,
        f"{name_coverage_bps / 100:.2f}%",
        ">= 98.00%",
        details=tuple(sorted(scope_symbols - named)[:50]),
    ))

    placeholder_rows = [
        f"{row.symbol}:{row.level1_name or ''}"
        for row in intervals
        if any(token in (row.level1_name or "") for token in ("未知", "申万一级", "申万二级", "申万三级"))
    ]
    results.append(GateResult(
        "no_fabricated_industry_names", not placeholder_rows, len(placeholder_rows), "= 0",
        details=tuple(placeholder_rows[:50]),
    ))

    verification_codes: dict[str, set[str]] = defaultdict(set)
    for row in verifications:
        if row.change_date <= as_of_date:
            verification_codes[row.symbol].add(row.industry_code)
    verified = {
        symbol for symbol, interval in latest_interval.items()
        if interval.industry_code in verification_codes.get(symbol, set())
    }
    verification_bps = len(verified) * 10000 // max(1, len(scope_symbols))
    results.append(GateResult(
        "cross_source_latest_code_coverage",
        verification_bps >= 9500,
        f"{verification_bps / 100:.2f}%",
        ">= 95.00%",
        details=tuple(sorted(scope_symbols - verified)[:50]),
    ))

    node_keys = [row.node_code for row in nodes]
    duplicate_nodes = sorted(code for code, count in Counter(node_keys).items() if count > 1)
    node_set = set(node_keys)
    missing_parents = sorted({
        row.parent_code for row in nodes
        if row.level > 0 and row.parent_code and row.parent_code not in node_set
    })
    results.append(GateResult(
        "classification_node_integrity",
        bool(nodes) and not duplicate_nodes and not missing_parents,
        len(nodes),
        "> 0 with unique nodes and complete parents",
        details=tuple([*duplicate_nodes[:25], *missing_parents[:25]]),
    ))
    current_level1 = [row for row in nodes if row.level == 1 and row.termination_date is None]
    results.append(GateResult(
        "sw2021_level1_inventory",
        len(current_level1) == 31,
        len(current_level1),
        "= 31",
    ))
    return results


def build_manifest(
    *,
    dataset_id: str,
    base_history_dataset_id: str,
    mode: str,
    observed_on: date,
    as_of_date: date,
    history_start: date,
    scope: Sequence[IndustryScopeSecurity],
    source_rows: Sequence[SwsAssignmentRecord],
    intervals: Sequence[IndustryInterval],
    verifications: Sequence[IndustryVerification],
    nodes: Sequence[IndustryNode],
    gates: Sequence[GateResult],
    source_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    interval_rows = [row.canonical() for row in sorted(intervals, key=lambda value: value.key)]
    verification_rows = [row.canonical() for row in sorted(verifications, key=lambda value: value.key)]
    node_rows = [row.canonical() for row in sorted(nodes, key=lambda value: (value.level, value.node_code))]
    source_assignment_rows = [row.canonical() for row in sorted(source_rows, key=lambda value: (value.symbol, value.source_effective_from))]
    gate_rows = [gate.canonical() for gate in gates]
    return {
        "manifest_version": MANIFEST_VERSION,
        "schema_version": INDUSTRY_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "base_history_dataset_id": base_history_dataset_id,
        "mode": mode,
        "observed_on": observed_on.isoformat(),
        "as_of_date": as_of_date.isoformat(),
        "history_start": history_start.isoformat(),
        "authoritative": False,
        "simulation_orders_allowed": False,
        "knowledge_boundary": {
            "historical_rows": "historical_reconstructed",
            "true_point_in_time_use_begins_on": observed_on.isoformat(),
        },
        "scope_count": len(scope),
        "source_assignment_count": len(source_assignment_rows),
        "interval_count": len(interval_rows),
        "verification_count": len(verification_rows),
        "node_count": len(node_rows),
        "scope_sha256": sha256(canonical_scope(scope)),
        "source_assignments_sha256": sha256(source_assignment_rows),
        "intervals_sha256": sha256(interval_rows),
        "verifications_sha256": sha256(verification_rows),
        "nodes_sha256": sha256(node_rows),
        "quality_sha256": sha256(gate_rows),
        "source_metadata": dict(sorted(source_metadata.items())),
        "accepted": accepted(gates),
        "gates": gate_rows,
    }


def write_gzip_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(list(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(payload)


def read_gzip_rows(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"invalid industry evidence file: {path}")
    return value

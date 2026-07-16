"""Fail-closed M2.2 gates for calendars and point-in-time constituents."""

from __future__ import annotations

from datetime import date
from typing import Iterable

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.quality_gates import GateResult
from scripts.market_data.sources.csi_index_source import EXPECTED_ATTACHMENT_HASHES, NOTICE_SPECS, REVIEWED_IRRELEVANT_NOTICE_IDS
from scripts.market_data.universe_contracts import INDEX_SIZES, UniverseEvent


def evaluate_calendars(primary: TradingCalendar, secondary: TradingCalendar) -> list[GateResult]:
    first = set(primary.open_dates)
    second = set(secondary.open_dates)
    missing = sorted(first - second)
    extra = sorted(second - first)
    return [
        GateResult("calendar_primary_nonempty", bool(first), len(first), "> 0"),
        GateResult("calendar_date_alignment", not missing and not extra, len(missing) + len(extra), "= 0", details=tuple(value.isoformat() for value in [*missing[:10], *extra[:10]])),
    ]


def evaluate_universe(
    events: Iterable[UniverseEvent],
    snapshots: dict[date, dict[str, tuple[str, ...]]],
    discovered_notice_ids: set[int],
    through: date,
) -> list[GateResult]:
    event_rows = list(events)
    expected_ids = {spec.notice_id for spec in NOTICE_SPECS if spec.stated_date <= through} | REVIEWED_IRRELEVANT_NOTICE_IDS
    results = [
        GateResult(
            "csi_notice_inventory",
            discovered_notice_ids == expected_ids,
            f"discovered={len(discovered_notice_ids)}, expected={len(expected_ids)}",
            "exact known official notice set",
            details=tuple(f"unexpected:{value}" for value in sorted(discovered_notice_ids - expected_ids)) + tuple(f"missing:{value}" for value in sorted(expected_ids - discovered_notice_ids)),
        )
    ]
    imbalanced: list[str] = []
    empty: list[str] = []
    hash_drift: list[str] = []
    for event in event_rows:
        if event.attachment_sha256 != EXPECTED_ATTACHMENT_HASHES.get(event.notice_id):
            hash_drift.append(f"{event.notice_id}:{event.attachment_sha256}")
        if not event.changes:
            empty.append(str(event.notice_id))
        for change in event.changes:
            if len(change.removed) != len(change.added):
                imbalanced.append(f"{event.notice_id}:{change.index_code}:{len(change.removed)}:{len(change.added)}")
    results.extend([
        GateResult("universe_event_nonempty", not empty, len(empty), "= 0", details=tuple(empty)),
        GateResult("universe_event_balanced", not imbalanced, len(imbalanced), "= 0", details=tuple(imbalanced)),
        GateResult("official_attachment_hashes", not hash_drift, len(hash_drift), "= 0", details=tuple(hash_drift)),
    ])
    size_errors: list[str] = []
    overlaps: list[str] = []
    for effective_date, members in sorted(snapshots.items()):
        for code, expected_size in INDEX_SIZES.items():
            actual = len(members.get(code, ()))
            if actual != expected_size:
                size_errors.append(f"{effective_date}:{code}:{actual}")
        overlap = set(members.get("000300", ())) & set(members.get("000905", ()))
        if overlap:
            overlaps.append(f"{effective_date}:{','.join(sorted(overlap)[:10])}")
    results.extend([
        GateResult("universe_snapshot_sizes", not size_errors, len(size_errors), "= 0", details=tuple(size_errors[:20])),
        GateResult("csi300_csi500_disjoint", not overlaps, len(overlaps), "= 0", details=tuple(overlaps[:20])),
    ])
    return results

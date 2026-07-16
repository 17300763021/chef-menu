"""Build deterministic M2.2 calendar and historical CSI 800 membership evidence."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

from scripts.market_data.manifest import sha256
from scripts.market_data.pit_quality_gates import evaluate_calendars, evaluate_universe
from scripts.market_data.quality_gates import accepted
from scripts.market_data.sources.akshare_calendar_source import AkshareCalendarSource
from scripts.market_data.sources.baostock_calendar_source import BaostockCalendarSource
from scripts.market_data.sources.csi_index_source import CsiIndexSource
from scripts.market_data.universe_contracts import CurrentUniverse, UniverseEvent


HISTORY_START = date(2018, 1, 1)


def reconstruct(current: CurrentUniverse, events: list[UniverseEvent]) -> dict[date, dict[str, tuple[str, ...]]]:
    state = {code: set(values) for code, values in current.members.items()}
    snapshots: dict[date, dict[str, tuple[str, ...]]] = {
        current.as_of_date: {code: tuple(sorted(values)) for code, values in state.items()}
    }
    for event in sorted(events, key=lambda value: (value.effective_session, value.notice_id), reverse=True):
        if event.effective_session > current.as_of_date:
            continue
        snapshots[event.effective_session] = {code: tuple(sorted(values)) for code, values in state.items()}
        for change in event.changes:
            additions = set(change.added)
            removals = set(change.removed)
            if not additions <= state[change.index_code]:
                raise ValueError(f"notice {event.notice_id} additions absent from post-event {change.index_code}")
            if removals & state[change.index_code]:
                raise ValueError(f"notice {event.notice_id} removals still present in post-event {change.index_code}")
            state[change.index_code].difference_update(additions)
            state[change.index_code].update(removals)
    snapshots[HISTORY_START] = {code: tuple(sorted(values)) for code, values in state.items()}
    return dict(sorted(snapshots.items()))


def snapshot_rows(snapshots: dict[date, dict[str, tuple[str, ...]]]) -> list[dict[str, Any]]:
    return [
        {"effective_session": effective.isoformat(), "index_code": code, "members": list(members)}
        for effective, universe in sorted(snapshots.items())
        for code, members in sorted(universe.items())
    ]


def run(end: date) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    primary = AkshareCalendarSource().fetch(HISTORY_START, end)
    secondary = BaostockCalendarSource().fetch(HISTORY_START, end)
    calendar_gates = evaluate_calendars(primary, secondary)
    if not accepted(calendar_gates):
        gates = calendar_gates
        rows: list[dict[str, Any]] = []
        events: list[UniverseEvent] = []
        current = None
        discovered: set[int] = set()
    else:
        csi = CsiIndexSource()
        current = csi.fetch_current()
        if current.as_of_date > end:
            raise ValueError(f"requested end {end} precedes current CSI snapshot {current.as_of_date}")
        events, discovered = csi.fetch_events(primary, current.as_of_date)
        snapshots = reconstruct(current, events)
        universe_gates = evaluate_universe(events, snapshots, discovered, current.as_of_date)
        gates = [*calendar_gates, *universe_gates]
        rows = snapshot_rows(snapshots)
    event_rows = [event.canonical() for event in events]
    manifest = {
        "manifest_version": "m2-pit-universe-manifest-v1",
        "authoritative": False,
        "simulation_orders_allowed": False,
        "history_start": HISTORY_START.isoformat(),
        "requested_end": end.isoformat(),
        "current_snapshot": None if current is None else current.canonical(),
        "source_versions": {
            "akshare": version("akshare"), "baostock": version("baostock"), "pandas": version("pandas"),
            "pdfplumber": version("pdfplumber"), "requests": version("requests"),
        },
        "calendar_sha256": sha256(primary.canonical()),
        "event_sha256": sha256(event_rows),
        "snapshot_sha256": sha256(rows),
        "event_count": len(event_rows),
        "snapshot_count": len(rows),
        "accepted": accepted(gates),
        "gates": [gate.canonical() for gate in gates],
        "events": event_rows,
    }
    return manifest, rows


def write_outputs(output_dir: Path, manifest: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with (output_dir / "csi800-pit-snapshots.json.gz").open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(payload)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="M2.2 point-in-time CSI 800 universe acceptance")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--output-dir", type=Path, default=Path("pit-universe-acceptance"))
    args = parser.parse_args()
    manifest, rows = run(args.end_date)
    write_outputs(args.output_dir, manifest, rows)
    print(json.dumps({
        "accepted": manifest["accepted"], "requested_end": manifest["requested_end"],
        "current_as_of": None if manifest["current_snapshot"] is None else manifest["current_snapshot"]["as_of_date"],
        "event_count": manifest["event_count"], "snapshot_count": manifest["snapshot_count"],
        "calendar_sha256": manifest["calendar_sha256"], "event_sha256": manifest["event_sha256"],
        "snapshot_sha256": manifest["snapshot_sha256"], "gates": manifest["gates"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["accepted"] else 2


if __name__ == "__main__":
    sys.exit(main())

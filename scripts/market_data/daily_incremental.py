"""Deterministic M2 daily-increment planning and evidence assembly.

Database publication and live-source orchestration remain separate adapters so
this module is deterministic and can be accepted with fixed fixtures.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from scripts.market_data.calendar_contracts import TradingCalendar
from scripts.market_data.contracts import DailyBar, canonical_rows, normalize_symbol
from scripts.market_data.daily_adjustments import PreviousAdjustedState, evaluate_daily_adjustments
from scripts.market_data.daily_quality_gates import evaluate_daily_incremental
from scripts.market_data.historical_contracts import AdjustmentEvent, HistoricalBar
from scripts.market_data.manifest import sha256
from scripts.market_data.pit_quality_gates import evaluate_calendars
from scripts.market_data.quality_gates import accepted
from scripts.market_data.tradeability_contracts import TradeabilityFact
from scripts.market_data.universe_contracts import INDEX_SIZES


DAILY_INCREMENTAL_SCHEMA_VERSION = "m2-daily-incremental-v3"
DAILY_INCREMENTAL_MANIFEST_VERSION = "m2-daily-incremental-manifest-v3"
SHANGHAI = ZoneInfo("Asia/Shanghai")
DATA_READY_TIME = time(16, 30)
DEFAULT_VERIFICATION_SYMBOLS = 40


@dataclass(frozen=True, slots=True)
class DailyIncrementalPlan:
    observed_at: datetime
    target_session: date
    previous_session: date
    snapshot_effective_session: date
    expected_membership: tuple[tuple[str, str], ...]
    accepted_existing_symbols: tuple[str, ...]
    fetch_symbols: tuple[str, ...]
    verification_symbols: tuple[str, ...]
    primary_calendar_sha256: str
    secondary_calendar_sha256: str
    universe_sha256: str
    schema_version: str = DAILY_INCREMENTAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        local_observed_at = _shanghai_time(self.observed_at)
        if datetime.combine(self.target_session, DATA_READY_TIME, tzinfo=SHANGHAI) > local_observed_at:
            raise ValueError("target session is not yet past the daily data-readiness cutoff")
        if self.previous_session >= self.target_session:
            raise ValueError("previous_session must precede target_session")
        if self.snapshot_effective_session > self.target_session:
            raise ValueError("point-in-time snapshot cannot be effective after the target session")
        symbols = [symbol for symbol, index_code in self.expected_membership if index_code in INDEX_SIZES]
        if len(symbols) != len(self.expected_membership) or len(set(symbols)) != len(symbols):
            raise ValueError("expected membership must contain unique symbols and supported indexes")
        if any(normalize_symbol(symbol) != symbol for symbol in symbols):
            raise ValueError("expected membership symbols must be normalized six-digit A-share codes")
        if tuple(sorted(self.expected_membership)) != self.expected_membership:
            raise ValueError("expected membership must be canonically sorted")
        expected = set(symbols)
        accepted_existing = set(self.accepted_existing_symbols)
        to_fetch = set(self.fetch_symbols)
        if tuple(sorted(accepted_existing)) != self.accepted_existing_symbols or tuple(sorted(to_fetch)) != self.fetch_symbols:
            raise ValueError("accepted-existing and fetch symbols must be unique and canonically sorted")
        if accepted_existing & to_fetch or accepted_existing | to_fetch != expected:
            raise ValueError("accepted-existing and fetch symbols must be a disjoint partition of the expected universe")
        if tuple(sorted(set(self.verification_symbols))) != self.verification_symbols:
            raise ValueError("verification symbols must be unique and canonically sorted")
        if not self.verification_symbols or not set(self.verification_symbols) <= expected:
            raise ValueError("verification symbols must be a nonempty subset of the expected universe")
        for value in (self.primary_calendar_sha256, self.secondary_calendar_sha256, self.universe_sha256):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("plan evidence hashes must be lowercase SHA-256 values")

    @property
    def membership(self) -> dict[str, str]:
        return dict(self.expected_membership)

    def scope_canonical(self) -> dict[str, Any]:
        """Return the stable business identity of one target-session capture.

        Observation-time provenance remains in ``canonical`` and the manifest,
        but it must not create a second checkpoint namespace when the target
        session, predecessor, membership, and verification sample are unchanged.
        """
        return {
            "schema_version": self.schema_version,
            "target_session": self.target_session.isoformat(),
            "previous_session": self.previous_session.isoformat(),
            "expected_membership": [
                {"symbol": symbol, "index_code": index_code}
                for symbol, index_code in self.expected_membership
            ],
            "verification_symbols": list(self.verification_symbols),
        }

    @property
    def scope_sha256(self) -> str:
        return sha256(self.scope_canonical())

    def canonical(self) -> dict[str, Any]:
        return {
            **self.scope_canonical(),
            "observed_at": self.observed_at.isoformat(),
            "snapshot_effective_session": self.snapshot_effective_session.isoformat(),
            "primary_calendar_sha256": self.primary_calendar_sha256,
            "secondary_calendar_sha256": self.secondary_calendar_sha256,
            "universe_sha256": self.universe_sha256,
            "accepted_existing_symbols": list(self.accepted_existing_symbols),
            "fetch_symbols": list(self.fetch_symbols),
            "scope_sha256": self.scope_sha256,
            "authoritative": False,
            "simulation_orders_allowed": False,
        }


def _shanghai_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return value.astimezone(SHANGHAI)


def latest_closed_session(
    calendar: TradingCalendar,
    observed_at: datetime,
    *,
    data_ready_time: time = DATA_READY_TIME,
) -> date:
    """Return the latest session whose post-close data-readiness time has passed."""
    local_time = _shanghai_time(observed_at)
    if calendar.end_date < local_time.date():
        raise ValueError(
            f"calendar horizon {calendar.end_date.isoformat()} is stale for {local_time.date().isoformat()}"
        )
    eligible = [
        session
        for session in calendar.open_dates
        if datetime.combine(session, data_ready_time, tzinfo=SHANGHAI) <= local_time
    ]
    if not eligible:
        raise ValueError("calendar contains no session whose daily data should be ready")
    return eligible[-1]


def _previous_session(calendar: TradingCalendar, target_session: date) -> date:
    earlier = [session for session in calendar.open_dates if session < target_session]
    if not earlier:
        raise ValueError(f"calendar contains no session before {target_session.isoformat()}")
    return earlier[-1]


def _verification_sample(symbols: list[str], maximum: int) -> tuple[str, ...]:
    if maximum < 1:
        raise ValueError("verification maximum must be positive")
    if len(symbols) <= maximum:
        return tuple(symbols)
    if maximum == 1:
        return (symbols[len(symbols) // 2],)
    positions = {round(index * (len(symbols) - 1) / (maximum - 1)) for index in range(maximum)}
    return tuple(symbols[index] for index in sorted(positions))


def _calendar_scope_sha256(calendar: TradingCalendar, target_session: date) -> str:
    """Hash only the accepted business scope so weekend retries keep one idempotency key."""
    return sha256({
        "schema_version": calendar.schema_version,
        "source": calendar.source,
        "through_session": target_session.isoformat(),
        "open_dates": [session.isoformat() for session in calendar.open_dates if session <= target_session],
    })


def _point_in_time_membership(
    target_session: date,
    snapshots: Mapping[date, Mapping[str, Iterable[str]]],
) -> tuple[date, tuple[tuple[str, str], ...], str]:
    eligible_dates = [effective for effective in snapshots if effective <= target_session]
    if not eligible_dates:
        raise ValueError(f"no point-in-time universe snapshot for {target_session.isoformat()}")
    effective = max(eligible_dates)
    snapshot = snapshots[effective]
    missing_indexes = sorted(set(INDEX_SIZES) - set(snapshot))
    if missing_indexes:
        raise ValueError(f"point-in-time universe is missing indexes: {missing_indexes}")

    normalized: dict[str, tuple[str, ...]] = {}
    size_errors: list[str] = []
    for index_code, expected_size in INDEX_SIZES.items():
        raw_members = tuple(normalize_symbol(value) for value in snapshot[index_code])
        members = tuple(sorted(set(raw_members)))
        normalized[index_code] = members
        if len(raw_members) != expected_size or len(members) != expected_size:
            size_errors.append(
                f"{index_code}:rows={len(raw_members)} unique={len(members)} expected {expected_size}"
            )
    if size_errors:
        raise ValueError(f"invalid point-in-time universe sizes: {size_errors}")
    overlap = set(normalized["000300"]) & set(normalized["000905"])
    if overlap:
        raise ValueError(f"CSI 300/500 universe overlap: {sorted(overlap)[:20]}")

    membership = tuple(sorted(
        (symbol, index_code)
        for index_code, members in normalized.items()
        for symbol in members
    ))
    universe_payload = {
        "effective_session": effective.isoformat(),
        "members": {index_code: list(normalized[index_code]) for index_code in sorted(normalized)},
    }
    return effective, membership, sha256(universe_payload)


def build_incremental_plan(
    *,
    observed_at: datetime,
    primary_calendar: TradingCalendar,
    secondary_calendar: TradingCalendar,
    snapshots: Mapping[date, Mapping[str, Iterable[str]]],
    accepted_existing_keys: Iterable[tuple[str, date]] = (),
    verification_maximum: int = DEFAULT_VERIFICATION_SYMBOLS,
    target_session: date | None = None,
) -> DailyIncrementalPlan:
    """Build a stable single-session scope and identify only missing symbols to fetch."""
    local_observed_at = _shanghai_time(observed_at)
    calendar_gates = evaluate_calendars(primary_calendar, secondary_calendar)
    if not accepted(calendar_gates):
        raise RuntimeError("primary and secondary trading calendars are not aligned")
    latest_primary = latest_closed_session(primary_calendar, local_observed_at)
    latest_secondary = latest_closed_session(secondary_calendar, local_observed_at)
    if latest_primary != latest_secondary:
        raise RuntimeError(
            f"latest closed session mismatch: {latest_primary.isoformat()} != {latest_secondary.isoformat()}"
        )
    primary_target = target_session or latest_primary
    if primary_target > latest_primary:
        raise ValueError(f"target session {primary_target.isoformat()} is not closed and ready")
    if primary_target not in primary_calendar.open_dates or primary_target not in secondary_calendar.open_dates:
        raise ValueError(f"target session {primary_target.isoformat()} is not aligned in both calendars")
    previous = _previous_session(primary_calendar, primary_target)
    effective, membership, universe_hash = _point_in_time_membership(primary_target, snapshots)
    expected_symbols = {symbol for symbol, _ in membership}
    present: set[str] = set()
    for symbol, business_date in accepted_existing_keys:
        if business_date != primary_target:
            continue
        normalized_symbol = normalize_symbol(symbol)
        if normalized_symbol in expected_symbols:
            present.add(normalized_symbol)
    sorted_symbols = sorted(expected_symbols)
    verification = _verification_sample(sorted_symbols, verification_maximum)
    return DailyIncrementalPlan(
        observed_at=local_observed_at,
        target_session=primary_target,
        previous_session=previous,
        snapshot_effective_session=effective,
        expected_membership=membership,
        accepted_existing_symbols=tuple(sorted(present)),
        fetch_symbols=tuple(symbol for symbol in sorted_symbols if symbol not in present),
        verification_symbols=verification,
        primary_calendar_sha256=_calendar_scope_sha256(primary_calendar, primary_target),
        secondary_calendar_sha256=_calendar_scope_sha256(secondary_calendar, primary_target),
        universe_sha256=universe_hash,
    )


BarFetcher = Callable[[str, date, date], list[DailyBar]]


def fetch_missing_bars(
    plan: DailyIncrementalPlan,
    fetcher: BarFetcher,
    *,
    attempts: int = 1,
) -> tuple[list[DailyBar], dict[str, str]]:
    """Fetch only plan-missing symbols through an injected adapter with bounded retries."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    rows: list[DailyBar] = []
    failures: dict[str, str] = {}
    for symbol in plan.fetch_symbols:
        last_error: Exception | None = None
        for _attempt in range(1, attempts + 1):
            try:
                fetched = fetcher(symbol, plan.target_session, plan.target_session)
                matching = [
                    row for row in fetched
                    if row.symbol == symbol and row.business_date == plan.target_session
                ]
                if len(fetched) != 1 or len(matching) != 1:
                    raise RuntimeError(
                        f"source must return exactly one target-session row for {symbol}; "
                        f"received {len(fetched)} rows and {len(matching)} matches"
                    )
                if matching[0].adjustment != "none":
                    raise RuntimeError(f"source returned a non-raw target row for {symbol}")
                rows.append(matching[0])
                last_error = None
                break
            except Exception as error:
                last_error = error
        if last_error is not None:
            failures[symbol] = f"{type(last_error).__name__}: {last_error}"
    return rows, failures


def build_incremental_evidence(
    *,
    plan: DailyIncrementalPlan,
    primary_bars: Iterable[DailyBar],
    tradeability_facts: Iterable[TradeabilityFact],
    verification_bars: Iterable[DailyBar],
    adjusted_bars: Iterable[HistoricalBar],
    adjustment_events: Iterable[AdjustmentEvent],
    previous_adjusted_states: Mapping[str, PreviousAdjustedState],
    accepted_previous_closes: Mapping[str, Decimal],
    reported_previous_closes: Mapping[str, Decimal],
    primary_failures: Mapping[str, str] | None = None,
    verification_failures: Mapping[str, str] | None = None,
) -> tuple[
    dict[str, Any], list[DailyBar], list[TradeabilityFact], list[DailyBar],
    list[HistoricalBar], list[AdjustmentEvent],
]:
    """Build deterministic non-authoritative evidence for one completed daily scope."""
    primary_rows = sorted(primary_bars, key=lambda row: (row.symbol, row.business_date, row.source))
    fact_rows = sorted(tradeability_facts, key=lambda row: (row.symbol, row.business_date))
    verification_rows = sorted(verification_bars, key=lambda row: (row.symbol, row.business_date, row.source))
    adjusted_rows = sorted(adjusted_bars, key=lambda row: (row.symbol, row.business_date))
    event_rows = sorted(adjustment_events, key=lambda row: (row.symbol, row.effective_date, row.source))
    gates = [*evaluate_daily_incremental(
        target_session=plan.target_session,
        previous_session=plan.previous_session,
        expected_membership=plan.membership,
        primary_bars=primary_rows,
        tradeability_facts=fact_rows,
        verification_bars=verification_rows,
        verification_symbols=plan.verification_symbols,
        accepted_previous_closes=accepted_previous_closes,
        reported_previous_closes=reported_previous_closes,
        primary_failures=primary_failures,
        verification_failures=verification_failures,
    ), *evaluate_daily_adjustments(
        target_session=plan.target_session,
        previous_session=plan.previous_session,
        membership=plan.membership,
        primary_bars=primary_rows,
        adjusted_bars=adjusted_rows,
        previous_states=previous_adjusted_states,
        reported_previous_closes=reported_previous_closes,
        adjustment_events=event_rows,
    )]
    canonical_primary = canonical_rows(primary_rows)
    canonical_facts = [row.canonical() for row in fact_rows]
    canonical_verification = canonical_rows(verification_rows)
    canonical_adjusted = [row.canonical() for row in adjusted_rows]
    canonical_events = [row.canonical() for row in event_rows]
    manifest = {
        "manifest_version": DAILY_INCREMENTAL_MANIFEST_VERSION,
        "schema_version": DAILY_INCREMENTAL_SCHEMA_VERSION,
        "authoritative": False,
        "simulation_orders_allowed": False,
        "observed_at": plan.observed_at.isoformat(),
        "target_session": plan.target_session.isoformat(),
        "previous_session": plan.previous_session.isoformat(),
        "snapshot_effective_session": plan.snapshot_effective_session.isoformat(),
        "scope_sha256": plan.scope_sha256,
        "expected_symbol_count": len(plan.expected_membership),
        "accepted_existing_symbol_count": len(plan.accepted_existing_symbols),
        "fetch_symbol_count": len(plan.fetch_symbols),
        "verification_symbol_count": len(plan.verification_symbols),
        "primary_calendar_sha256": plan.primary_calendar_sha256,
        "secondary_calendar_sha256": plan.secondary_calendar_sha256,
        "universe_sha256": plan.universe_sha256,
        "expected_membership_sha256": sha256([
            {"symbol": symbol, "index_code": index_code}
            for symbol, index_code in plan.expected_membership
        ]),
        "primary_row_count": len(primary_rows),
        "tradeability_row_count": len(fact_rows),
        "verification_row_count": len(verification_rows),
        "adjusted_row_count": len(adjusted_rows),
        "adjustment_event_count": len(event_rows),
        "primary_failures": dict(sorted((primary_failures or {}).items())),
        "verification_failures": dict(sorted((verification_failures or {}).items())),
        "primary_sha256": sha256(canonical_primary),
        "tradeability_sha256": sha256(canonical_facts),
        "verification_sha256": sha256(canonical_verification),
        "adjusted_sha256": sha256(canonical_adjusted),
        "adjustments_sha256": sha256(canonical_events),
        "quality_sha256": sha256([gate.canonical() for gate in gates]),
        "accepted": accepted(gates),
        "gates": [gate.canonical() for gate in gates],
    }
    return manifest, primary_rows, fact_rows, verification_rows, adjusted_rows, event_rows


def _write_gzip(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(payload)


def write_outputs(
    output_dir: Path,
    manifest: Mapping[str, Any],
    primary_bars: Iterable[DailyBar],
    tradeability_facts: Iterable[TradeabilityFact],
    verification_bars: Iterable[DailyBar],
    adjusted_bars: Iterable[HistoricalBar],
    adjustment_events: Iterable[AdjustmentEvent],
) -> None:
    canonical_primary = canonical_rows(primary_bars)
    canonical_facts = [
        row.canonical()
        for row in sorted(tradeability_facts, key=lambda value: (value.symbol, value.business_date))
    ]
    canonical_verification = canonical_rows(verification_bars)
    canonical_adjusted = [
        row.canonical()
        for row in sorted(adjusted_bars, key=lambda value: (value.symbol, value.business_date))
    ]
    canonical_events = [
        row.canonical()
        for row in sorted(adjustment_events, key=lambda value: (value.symbol, value.effective_date, value.source))
    ]
    expected_evidence = {
        "primary_row_count": len(canonical_primary),
        "tradeability_row_count": len(canonical_facts),
        "verification_row_count": len(canonical_verification),
        "adjusted_row_count": len(canonical_adjusted),
        "adjustment_event_count": len(canonical_events),
        "primary_sha256": sha256(canonical_primary),
        "tradeability_sha256": sha256(canonical_facts),
        "verification_sha256": sha256(canonical_verification),
        "adjusted_sha256": sha256(canonical_adjusted),
        "adjustments_sha256": sha256(canonical_events),
    }
    mismatches = {
        key: {"manifest": manifest.get(key), "actual": value}
        for key, value in expected_evidence.items()
        if manifest.get(key) != value
    }
    if mismatches:
        raise ValueError(f"manifest does not match daily evidence: {mismatches}")

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_gzip(output_dir / "daily-primary-bars.json.gz", canonical_primary)
    _write_gzip(output_dir / "daily-tradeability.json.gz", canonical_facts)
    _write_gzip(output_dir / "daily-verification-bars.json.gz", canonical_verification)
    _write_gzip(output_dir / "daily-adjusted-bars.json.gz", canonical_adjusted)
    _write_gzip(output_dir / "daily-adjustment-events.json.gz", canonical_events)
    (output_dir / "manifest.json").write_text(
        json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

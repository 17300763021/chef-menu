"""Online M2 daily-increment acquisition with TiDB checkpoints.

The runner advances exactly one trading session from the last accepted lineage.
It never skips a missing session, never publishes a partial aggregate, and every
stored result remains research-only with simulated-order permission disabled.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from scripts.market_data.contracts import DailyBar, parse_date
from scripts.market_data.daily_adjustments import (
    PreviousAdjustedState,
    build_daily_adjusted_bars,
    has_price_break,
)
from scripts.market_data.daily_incremental import (
    DailyIncrementalPlan,
    build_incremental_evidence,
    build_incremental_plan,
    latest_closed_session,
    write_outputs,
)
from scripts.market_data.historical_bars import load_calendars
from scripts.market_data.historical_contracts import AdjustmentEvent, HistoricalBar
from scripts.market_data.pit_universe import reconstruct
from scripts.market_data.quality_gates import accepted
from scripts.market_data.sources.akshare_history_source import (
    AkshareEastmoneyHistorySource,
    AkshareHistorySource,
)
from scripts.market_data.sources.baostock_history_source import BaostockHistorySource
from scripts.market_data.sources.csi_index_source import CsiIndexSource
from scripts.market_data.tidb_daily_store import (
    DailyEvidence,
    TiDBConfig,
    connect,
    default_daily_dataset_id,
    ensure_daily_schema,
    latest_accepted_lineage,
    load_base_references,
    load_daily_checkpoint_evidence,
    load_previous_adjusted_states,
    publish_daily_run,
    publish_daily_symbol_checkpoint,
)
from scripts.market_data.tradeability import derive_tradeability
from scripts.market_data.tradeability_contracts import TradeabilityFact


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_BASE_HISTORY_DATASET_ID = (
    "m2-full-2026-07-24-"
    "993df9aab3cbd021a495535c9326eaa79f26f4bbfbe74b28215256e778e517f7-merged"
)
SYMBOL_DEADLINE_SECONDS = 90


class DailySymbolTimeout(BaseException):
    pass


@contextmanager
def symbol_deadline(seconds: int):
    """Use a non-swallowable alarm on Linux; Windows remains local-test only."""
    try:
        import signal
    except ImportError:
        yield
        return
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def raise_timeout(signum: int, frame: object) -> None:
        raise DailySymbolTimeout(f"daily symbol acquisition exceeded {seconds} seconds")

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _progress(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False, sort_keys=True), flush=True)


def _one_target_row(rows: Iterable[DailyBar], symbol: str, target: date, label: str) -> DailyBar:
    values = list(rows)
    matches = [row for row in values if row.symbol == symbol and row.business_date == target]
    if len(values) != 1 or len(matches) != 1:
        raise RuntimeError(
            f"{label} must return exactly one target row for {symbol}; "
            f"received {len(values)} rows and {len(matches)} matches"
        )
    return matches[0]


def _listing_age(ipo_date: date, session: date, calendar_dates: tuple[date, ...]) -> int:
    return max(
        0,
        bisect.bisect_right(calendar_dates, session)
        - bisect.bisect_right(calendar_dates, ipo_date - timedelta(days=1)),
    )


def _tradeability_row(bar: DailyBar | None) -> dict[str, object] | None:
    if bar is None:
        return None
    return {"high": bar.high, "low": bar.low, "close": bar.close}


def _daily_bar_from_canonical(row: dict[str, Any]) -> DailyBar:
    return DailyBar(
        source=str(row["source"]), symbol=str(row["symbol"]), exchange=str(row["exchange"]),
        business_date=parse_date(row["business_date"]), open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])), low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        previous_close=None if row.get("previous_close") is None else Decimal(str(row["previous_close"])),
        volume_shares=int(row["volume_shares"]), amount_cny=Decimal(str(row["amount_cny"])),
        turnover_percent=None if row.get("turnover_percent") is None else Decimal(str(row["turnover_percent"])),
        trade_status=str(row["trade_status"]), is_st=row.get("is_st"),
        adjustment=str(row.get("adjustment", "none")), schema_version=str(row["schema_version"]),
    )


def _historical_bar_from_canonical(row: dict[str, Any]) -> HistoricalBar:
    return HistoricalBar(
        symbol=str(row["symbol"]), exchange=str(row["exchange"]),
        business_date=parse_date(row["business_date"]), index_code=str(row["index_code"]),
        open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])), close=Decimal(str(row["close"])),
        previous_close=None if row.get("previous_close") is None else Decimal(str(row["previous_close"])),
        volume_shares=int(row["volume_shares"]), amount_cny=Decimal(str(row["amount_cny"])),
        turnover_percent=None if row.get("turnover_percent") is None else Decimal(str(row["turnover_percent"])),
        qfq_factor=Decimal(str(row["qfq_factor"])), hfq_factor=Decimal(str(row["hfq_factor"])),
        qfq_open=Decimal(str(row["qfq_open"])), qfq_high=Decimal(str(row["qfq_high"])),
        qfq_low=Decimal(str(row["qfq_low"])), qfq_close=Decimal(str(row["qfq_close"])),
        hfq_open=Decimal(str(row["hfq_open"])), hfq_high=Decimal(str(row["hfq_high"])),
        hfq_low=Decimal(str(row["hfq_low"])), hfq_close=Decimal(str(row["hfq_close"])),
        primary_source=str(row["primary_source"]), factor_source=str(row["factor_source"]),
        schema_version=str(row["schema_version"]),
    )


def _fact_from_canonical(row: dict[str, Any]) -> TradeabilityFact:
    return TradeabilityFact(
        symbol=str(row["symbol"]), business_date=parse_date(row["business_date"]),
        index_code=str(row["index_code"]), has_primary_bar=bool(row["has_primary_bar"]),
        has_secondary_status=bool(row["has_secondary_status"]),
        is_suspended=bool(row["is_suspended"]), is_st=row.get("is_st"),
        listing_age_sessions=int(row["listing_age_sessions"]),
        limit_rate=None if row.get("limit_rate") is None else Decimal(str(row["limit_rate"])),
        limit_up=None if row.get("limit_up") is None else Decimal(str(row["limit_up"])),
        limit_down=None if row.get("limit_down") is None else Decimal(str(row["limit_down"])),
        at_limit_up=bool(row["at_limit_up"]), at_limit_down=bool(row["at_limit_down"]),
        one_price_limit_up=bool(row["one_price_limit_up"]),
        one_price_limit_down=bool(row["one_price_limit_down"]),
        can_buy=bool(row["can_buy"]), can_sell=bool(row["can_sell"]),
        block_reasons=tuple(row["block_reasons"]), schema_version=str(row["schema_version"]),
    )


def _event_from_canonical(row: dict[str, Any]) -> AdjustmentEvent:
    return AdjustmentEvent(
        symbol=str(row["symbol"]), effective_date=parse_date(row["effective_date"]),
        qfq_factor=Decimal(str(row["qfq_factor"])), hfq_factor=Decimal(str(row["hfq_factor"])),
        source=str(row["source"]),
    )


def _checkpoint_evidence(
    *,
    primary: DailyBar | None,
    fact: TradeabilityFact | None,
    verification: DailyBar | None,
    adjusted: HistoricalBar | None,
    events: Iterable[AdjustmentEvent],
) -> DailyEvidence:
    return DailyEvidence(
        manifest={"authoritative": False, "simulation_orders_allowed": False},
        primary_bars=[] if primary is None else [primary.canonical()],
        tradeability=[] if fact is None else [fact.canonical()],
        verification_bars=[] if verification is None else [verification.canonical()],
        adjusted_bars=[] if adjusted is None else [adjusted.canonical()],
        adjustments=[event.canonical() for event in events],
    )


def _target_events(
    source: AkshareEastmoneyHistorySource,
    symbol: str,
    target: date,
) -> list[AdjustmentEvent]:
    return [
        event for event in source.fetch_sina_adjustments(symbol, target)
        if event.effective_date == target
    ]


def capture_symbol(
    *,
    plan: DailyIncrementalPlan,
    symbol: str,
    primary_source: AkshareEastmoneyHistorySource,
    verification_source: AkshareHistorySource,
    secondary_source: BaostockHistorySource,
    previous_states: dict[str, PreviousAdjustedState],
    ipo_dates: dict[str, date],
    calendar_dates: tuple[date, ...],
) -> tuple[DailyEvidence, Decimal | None, str, Exception | None]:
    """Capture one symbol and classify its resumable checkpoint state."""
    target = plan.target_session
    verification_required = symbol in set(plan.verification_symbols)
    status_rows = secondary_source.fetch_status(symbol, target, target)
    secondary = status_rows.get(target)
    if secondary is None:
        raise RuntimeError(f"secondary status missing for {symbol}:{target.isoformat()}")
    reported = Decimal(str(secondary["preclose"])) if str(secondary.get("preclose", "")).strip() else None
    trade_status = str(secondary.get("tradestatus", "")).strip()
    if trade_status not in {"0", "1"}:
        raise RuntimeError(f"secondary trade status is unknown for {symbol}:{target.isoformat()}")
    ipo_date = ipo_dates.get(symbol)
    if ipo_date is None:
        raise RuntimeError(f"base security reference missing for {symbol}")

    primary: DailyBar | None = None
    adjusted: HistoricalBar | None = None
    verification: DailyBar | None = None
    events: list[AdjustmentEvent] = []
    recoverable_error: Exception | None = None
    primary_source_name: str | None = None
    if trade_status == "1":
        try:
            raw_map, primary_source_name = primary_source.fetch_raw_with_fallback(symbol, target, target)
            primary = _one_target_row(raw_map.values(), symbol, target, "primary source")
        except Exception as error:
            recoverable_error = error

    age = _listing_age(ipo_date, target, calendar_dates)
    fact = derive_tradeability(
        symbol=symbol, business_date=target, index_code=plan.membership[symbol],
        listing_age_sessions=age, primary=_tradeability_row(primary), secondary=secondary,
    )
    if primary is None:
        if fact.is_suspended and fact.has_secondary_status:
            return _checkpoint_evidence(
                primary=None, fact=fact, verification=None, adjusted=None, events=[],
            ), reported, "succeeded", None
        error = recoverable_error or RuntimeError(f"active primary bar missing for {symbol}")
        return _checkpoint_evidence(
            primary=None, fact=fact, verification=None, adjusted=None, events=[],
        ), reported, "blocked", error

    state = previous_states.get(symbol)
    if state is None or state.business_date != plan.previous_session:
        error = RuntimeError(f"exact predecessor adjusted state missing for {symbol}")
        blocked_fact = derive_tradeability(
            symbol=symbol, business_date=target, index_code=plan.membership[symbol],
            listing_age_sessions=age, primary=None, secondary=secondary,
        )
        blocked_fact = replace(
            blocked_fact,
            can_buy=False,
            can_sell=False,
            block_reasons=tuple(sorted(set(blocked_fact.block_reasons) | {"missing_adjustment_predecessor"})),
        )
        return _checkpoint_evidence(
            primary=None, fact=blocked_fact, verification=None, adjusted=None, events=[],
        ), reported, "blocked", error
    if reported is None or reported <= 0:
        raise RuntimeError(f"positive reported previous close missing for {symbol}")
    try:
        if has_price_break(state.raw_close, reported):
            events = _target_events(primary_source, symbol, target)
        adjusted_rows = build_daily_adjusted_bars(
            target_session=target, previous_session=plan.previous_session,
            membership=plan.membership, primary_bars=[primary], previous_states={symbol: state},
            reported_previous_closes={symbol: reported}, adjustment_events=events,
        )
        adjusted = adjusted_rows[0]
    except Exception as error:
        blocked_fact = derive_tradeability(
            symbol=symbol, business_date=target, index_code=plan.membership[symbol],
            listing_age_sessions=age, primary=None, secondary=secondary,
        )
        blocked_fact = replace(
            blocked_fact,
            can_buy=False,
            can_sell=False,
            block_reasons=tuple(sorted(set(blocked_fact.block_reasons) | {"invalid_adjustment_continuity"})),
        )
        return _checkpoint_evidence(
            primary=None, fact=blocked_fact, verification=None, adjusted=None, events=[],
        ), reported, "blocked", error
    if verification_required:
        try:
            values = verification_source.fetch_raw(
                symbol, target, target, exclude_sources={str(primary_source_name or primary.source)},
            )
            verification = _one_target_row(values, symbol, target, "verification source")
        except Exception as error:
            recoverable_error = error

    evidence = _checkpoint_evidence(
        primary=primary, fact=fact, verification=verification, adjusted=adjusted, events=events,
    )
    if verification_required and verification is None:
        assert recoverable_error is not None
        return evidence, reported, "blocked", recoverable_error
    return evidence, reported, "succeeded", None


def _select_target(
    calendar_dates: tuple[date, ...],
    latest_accepted: date,
    latest_ready: date,
    requested: date | None,
) -> date | None:
    pending = [value for value in calendar_dates if latest_accepted < value <= latest_ready]
    if not pending:
        return None
    next_session = pending[0]
    if requested is not None and requested != next_session:
        raise RuntimeError(
            f"daily lineage cannot skip sessions: next={next_session.isoformat()} requested={requested.isoformat()}"
        )
    return requested or next_session


def run(
    *,
    observed_at: datetime,
    base_history_dataset_id: str,
    output_dir: Path,
    requested_target: date | None = None,
    initialize_schema: bool = False,
    symbol_attempts: int = 2,
) -> dict[str, Any]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if symbol_attempts < 1 or symbol_attempts > 3:
        raise ValueError("symbol attempts must be between 1 and 3")
    observed = observed_at.astimezone(SHANGHAI)
    primary_calendar, secondary_calendar, calendar_gates, _sources = load_calendars(observed.date())
    if not accepted(calendar_gates):
        raise RuntimeError("daily primary and secondary calendars are not aligned")

    config = TiDBConfig.from_env()
    connection = connect(config)
    try:
        if initialize_schema:
            ensure_daily_schema(connection)
        latest_accepted, predecessor_dataset_id = latest_accepted_lineage(
            connection, base_history_dataset_id,
        )
        latest_ready = latest_closed_session(primary_calendar, observed)
    finally:
        connection.close()

    target = _select_target(primary_calendar.open_dates, latest_accepted, latest_ready, requested_target)
    if target is None:
        result = {
            "event": "daily_noop", "latest_accepted_session": latest_accepted.isoformat(),
            "latest_ready_session": latest_ready.isoformat(), "simulation_orders_allowed": False,
        }
        _progress(**result)
        return result

    csi = CsiIndexSource()
    current = csi.fetch_current()
    events, discovered, _event_source = csi.fetch_indexed_events(current.as_of_date)
    snapshots = reconstruct(current, events)
    base_plan = build_incremental_plan(
        observed_at=observed, primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar, snapshots=snapshots, target_session=target,
    )
    dataset_id = default_daily_dataset_id(target, base_plan.scope_sha256)

    connection = connect(config)
    try:
        if initialize_schema:
            ensure_daily_schema(connection)
        stored, metadata = load_daily_checkpoint_evidence(connection, dataset_id)
        plan = build_incremental_plan(
            observed_at=observed, primary_calendar=primary_calendar,
            secondary_calendar=secondary_calendar, snapshots=snapshots,
            accepted_existing_keys=(
                (symbol, target) for symbol in metadata["succeeded_symbols"]
            ),
            target_session=target,
        )
        previous_states = load_previous_adjusted_states(
            connection, predecessor_dataset_id=predecessor_dataset_id,
            previous_session=plan.previous_session,
        )
        ipo_dates = load_base_references(connection, base_history_dataset_id)
    finally:
        connection.close()

    _progress(
        "daily_scope_ready", dataset_id=dataset_id, target_session=target.isoformat(),
        predecessor_dataset_id=predecessor_dataset_id,
        expected_symbols=len(plan.expected_membership), resumed_symbols=len(plan.accepted_existing_symbols),
        fetch_symbols=len(plan.fetch_symbols), verification_symbols=len(plan.verification_symbols),
    )

    primary_source = AkshareEastmoneyHistorySource(timeout_seconds=25, attempts=2)
    verification_source = AkshareHistorySource(timeout_seconds=25, attempts=2)
    secondary_context = (
        BaostockHistorySource(timeout_seconds=25, attempts=2)
        if plan.fetch_symbols else nullcontext(None)
    )
    with secondary_context as secondary_source:
        for position, symbol in enumerate(plan.fetch_symbols, start=1):
            assert secondary_source is not None
            final_error: Exception | str | None = None
            for attempt in range(1, symbol_attempts + 1):
                try:
                    with symbol_deadline(SYMBOL_DEADLINE_SECONDS):
                        evidence, reported, status, error = capture_symbol(
                            plan=plan, symbol=symbol, primary_source=primary_source,
                            verification_source=verification_source, secondary_source=secondary_source,
                            previous_states=previous_states, ipo_dates=ipo_dates,
                            calendar_dates=primary_calendar.open_dates,
                        )
                    final_error = error
                    if status == "blocked" and attempt < symbol_attempts:
                        _progress(
                            "daily_symbol_retry", symbol=symbol, attempt=attempt,
                            error=f"{type(error).__name__}: {error}" if error else "blocked",
                        )
                        time.sleep(min(2 ** (attempt - 1), 2))
                        continue
                    checkpoint_connection = connect(config)
                    try:
                        publish_daily_symbol_checkpoint(
                            checkpoint_connection, evidence, dataset_id=dataset_id,
                            symbol=symbol, target_session=target,
                            verification_required=symbol in set(plan.verification_symbols),
                            reported_previous_close=reported, status=status, error=error,
                        )
                    finally:
                        checkpoint_connection.close()
                    break
                except DailySymbolTimeout as error:
                    final_error = RuntimeError(str(error))
                except Exception as error:
                    final_error = error
                if attempt < symbol_attempts:
                    _progress(
                        "daily_symbol_retry", symbol=symbol, attempt=attempt,
                        error=f"{type(final_error).__name__}: {final_error}",
                    )
                    time.sleep(min(2 ** (attempt - 1), 2))
            else:
                failed_connection = connect(config)
                try:
                    publish_daily_symbol_checkpoint(
                        failed_connection,
                        _checkpoint_evidence(
                            primary=None, fact=None, verification=None, adjusted=None, events=[],
                        ),
                        dataset_id=dataset_id, symbol=symbol, target_session=target,
                        verification_required=symbol in set(plan.verification_symbols),
                        reported_previous_close=None, status="failed", error=final_error or "unknown failure",
                    )
                finally:
                    failed_connection.close()
            _progress(
                "daily_symbol_completed", symbol=symbol, completed=position,
                total=len(plan.fetch_symbols),
            )

    connection = connect(config)
    try:
        stored, metadata = load_daily_checkpoint_evidence(connection, dataset_id)
    finally:
        connection.close()
    primary_rows = [_daily_bar_from_canonical(row) for row in stored.primary_bars]
    fact_rows = [_fact_from_canonical(row) for row in stored.tradeability]
    verification_rows = [_daily_bar_from_canonical(row) for row in stored.verification_bars]
    adjusted_rows = [_historical_bar_from_canonical(row) for row in stored.adjusted_bars]
    event_rows = [_event_from_canonical(row) for row in stored.adjustments]
    reported_closes = metadata["reported_previous_closes"]
    accepted_closes = {
        symbol: state.raw_close for symbol, state in previous_states.items()
        if symbol in plan.membership
    }
    primary_failures = {
        symbol: message for symbol, message in metadata["errors"].items()
        if symbol not in {row.symbol for row in primary_rows}
    }
    verification_failures = {
        symbol: metadata["errors"].get(symbol, "RuntimeError: verification missing")
        for symbol in plan.verification_symbols
        if symbol in {row.symbol for row in primary_rows}
        and symbol not in {row.symbol for row in verification_rows}
    }
    manifest, primary_rows, fact_rows, verification_rows, adjusted_rows, event_rows = build_incremental_evidence(
        plan=plan, primary_bars=primary_rows, tradeability_facts=fact_rows,
        verification_bars=verification_rows, adjusted_bars=adjusted_rows,
        adjustment_events=event_rows, previous_adjusted_states=previous_states,
        accepted_previous_closes=accepted_closes,
        reported_previous_closes=reported_closes,
        primary_failures=primary_failures, verification_failures=verification_failures,
    )
    manifest.update({
        "dataset_id": dataset_id,
        "base_history_dataset_id": base_history_dataset_id,
        "predecessor_dataset_id": predecessor_dataset_id,
        "source_versions": {
            "akshare": version("akshare"), "baostock": version("baostock"),
        },
        "csi_discovered_notice_ids": sorted(discovered),
        "checkpoint_succeeded_symbol_count": len(metadata["succeeded_symbols"]),
        "checkpoint_blocked_symbol_count": len(metadata["blocked_symbols"]),
    })
    write_outputs(
        output_dir, manifest, primary_rows, fact_rows, verification_rows,
        adjusted_rows, event_rows,
    )
    if not manifest["accepted"]:
        failed = [gate for gate in manifest["gates"] if gate["critical"] and not gate["passed"]]
        _progress(
            "daily_rejected", dataset_id=dataset_id, target_session=target.isoformat(),
            failed_critical_gates=[gate["name"] for gate in failed],
        )
        return {"dataset_id": dataset_id, "accepted": False, "manifest": manifest}

    publication = DailyEvidence(
        manifest=manifest,
        primary_bars=[row.canonical() for row in primary_rows],
        tradeability=[row.canonical() for row in fact_rows],
        verification_bars=[row.canonical() for row in verification_rows],
        adjusted_bars=[row.canonical() for row in adjusted_rows],
        adjustments=[row.canonical() for row in event_rows],
    )
    connection = connect(config)
    try:
        result = publish_daily_run(
            connection, publication, dataset_id=dataset_id,
            base_history_dataset_id=base_history_dataset_id,
            predecessor_dataset_id=predecessor_dataset_id,
        )
    finally:
        connection.close()
    _progress("daily_accepted", **result, target_session=target.isoformat())
    return {**result, "manifest": manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description="M2 daily point-in-time market increment")
    parser.add_argument(
        "--base-history-dataset-id",
        default=os.environ.get("M2_BASE_HISTORY_DATASET_ID", "").strip() or DEFAULT_BASE_HISTORY_DATASET_ID,
    )
    parser.add_argument("--target-session", type=date.fromisoformat)
    parser.add_argument("--observed-at", type=datetime.fromisoformat)
    parser.add_argument("--output-dir", type=Path, default=Path("daily-market-increment"))
    parser.add_argument("--init-schema", action="store_true")
    parser.add_argument("--symbol-attempts", type=int, default=2)
    args = parser.parse_args()
    observed_at = args.observed_at or datetime.now(SHANGHAI)
    result = run(
        observed_at=observed_at, base_history_dataset_id=args.base_history_dataset_id,
        output_dir=args.output_dir, requested_target=args.target_session,
        initialize_schema=args.init_schema, symbol_attempts=args.symbol_attempts,
    )
    return 0 if result.get("accepted", result.get("event") == "daily_noop") else 2


if __name__ == "__main__":
    sys.exit(main())

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
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from scripts.market_data.contracts import DailyBar, parse_date
from scripts.market_data.daily_adjustments import (
    PreviousAdjustedState,
    RQALPHA_DEFERRED_CASH_ACTION_SOURCE,
    build_daily_adjusted_bars,
    has_price_break,
)
from scripts.market_data.daily_incremental import (
    DailyIncrementalPlan,
    build_incremental_evidence,
    build_incremental_plan,
    latest_closed_session,
    validate_daily_calendar_boundary,
    write_outputs,
)
from scripts.market_data.daily_quality_gates import cross_source_consistency_errors
from scripts.market_data.historical_bars import load_calendars
from scripts.market_data.historical_contracts import AdjustmentEvent, HistoricalBar
from scripts.market_data.manifest import sha256
from scripts.market_data.pit_universe import reconstruct
from scripts.market_data.sources.akshare_history_source import (
    AkshareEastmoneyHistorySource,
    AkshareHistorySource,
    SinaFactorsUnavailableError,
)
from scripts.market_data.sources.baostock_history_source import BaostockHistorySource
from scripts.market_data.sources.csi_index_source import CsiIndexSource
from scripts.market_data.sources.eastmoney_corporate_action_source import EastmoneyCorporateActionSource
from scripts.market_data.sources.eastmoney_market_state_source import EastmoneySuspensionSource
from scripts.market_data.sources.tencent_history_source import TencentHistorySource
from scripts.market_data.tidb_daily_store import (
    DailyEvidence,
    TiDBConfig,
    canonical_lineage_evidence,
    connect,
    daily_correction_context,
    default_daily_dataset_id,
    ensure_daily_schema,
    latest_accepted_lineage,
    load_base_references,
    load_daily_checkpoint_evidence,
    load_latest_prior_adjusted_states,
    load_previous_adjusted_states,
    publish_daily_run,
    publish_daily_symbol_checkpoint,
    recover_compatible_daily_checkpoints,
    recovered_previous_states_from_lineage,
)
from scripts.market_data.tradeability import derive_tradeability
from scripts.market_data.tradeability_contracts import TradeabilityFact


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_BASE_HISTORY_DATASET_ID = (
    "m2-full-2026-07-24-"
    "993df9aab3cbd021a495535c9326eaa79f26f4bbfbe74b28215256e778e517f7-merged"
)
SYMBOL_DEADLINE_SECONDS = 90
CALENDAR_DEADLINE_SECONDS = 180


def daily_membership_symbols(expected_membership: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(sorted(symbol for symbol, _index_code in expected_membership))


def select_daily_shard_symbols(
    scope_symbols: Iterable[str], pending_symbols: Iterable[str],
    shard_index: int, shard_count: int,
) -> tuple[str, ...]:
    """Select a stable shard from the full scope, independent of checkpoint timing."""
    if shard_count < 1 or shard_count > 4 or not 0 <= shard_index < shard_count:
        raise ValueError("daily shard coordinates must use between 1 and 4 stable shards")
    assigned = set(sorted(scope_symbols)[shard_index::shard_count])
    return tuple(symbol for symbol in sorted(set(pending_symbols)) if symbol in assigned)


class DailySymbolTimeout(BaseException):
    pass


class DailyPrerequisiteTimeout(BaseException):
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


@contextmanager
def prerequisite_deadline(seconds: int):
    """Bound cloud-only prerequisite calls that may block inside vendor sockets."""
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
        raise DailyPrerequisiteTimeout(
            f"daily calendar prerequisites exceeded {seconds} seconds"
        )

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _progress(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False, sort_keys=True), flush=True)


def _progress_result(result: dict[str, Any]) -> None:
    """Emit a result whose event name is already part of the payload."""
    event = result.get("event")
    if not isinstance(event, str) or not event:
        raise ValueError("progress result must contain a non-empty event")
    values = dict(result)
    values.pop("event", None)
    _progress(event, **values)


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
    status_source: str | None = None,
    previous_close_source: str | None = None,
    lineage_evidence: Iterable[dict[str, Any]] = (),
) -> DailyEvidence:
    return DailyEvidence(
        manifest={
            "authoritative": False,
            "simulation_orders_allowed": False,
            "status_source": status_source,
            "previous_close_source": previous_close_source,
        },
        primary_bars=[] if primary is None else [primary.canonical()],
        tradeability=[] if fact is None else [fact.canonical()],
        verification_bars=[] if verification is None else [verification.canonical()],
        adjusted_bars=[] if adjusted is None else [adjusted.canonical()],
        adjustments=[event.canonical() for event in events],
        lineage_evidence=sorted(
            (canonical_lineage_evidence(row) for row in lineage_evidence),
            key=lambda row: (row["symbol"], row["kind"]),
        ),
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


def _factor_reference_closes(
    lineage_evidence: Iterable[Mapping[str, Any]],
) -> dict[str, Decimal]:
    """Rebuild exact factor references from validated cash-dividend lineage."""
    references: dict[str, Decimal] = {}
    for raw in lineage_evidence:
        row = canonical_lineage_evidence(raw)
        if row["kind"] != "cash_dividend_reference":
            continue
        details = row["details"]
        accepted_close = Decimal(str(details["accepted_previous_close"]))
        cash_per_ten = Decimal(str(details["cash_per_ten_shares"]))
        reference = accepted_close - cash_per_ten / Decimal("10")
        recorded = details.get("factor_reference_close")
        if recorded is not None and Decimal(str(recorded)) != reference:
            raise ValueError(
                f"cash-dividend factor reference does not reconcile for {row['symbol']}"
            )
        existing = references.get(row["symbol"])
        if existing is not None and existing != reference:
            raise ValueError(f"conflicting cash-dividend factor references for {row['symbol']}")
        references[row["symbol"]] = reference
    return references


def _verified_cash_dividend_lineage(
    *,
    symbol: str,
    previous_session: date,
    target_session: date,
    accepted_previous_close: Decimal,
    reported_previous_close: Decimal,
    action_details: Mapping[str, Any],
    corporate_action_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build cash lineage after exact point-in-time source reconciliation."""
    details = dict(action_details)
    if parse_date(details.get("previous_session")) != previous_session:
        raise RuntimeError(f"Tencent previous session does not match for {symbol}")
    if parse_date(details.get("registration_date")) != previous_session:
        raise RuntimeError(f"Tencent registration date does not match for {symbol}")
    if parse_date(details.get("ex_rights_date")) != target_session:
        raise RuntimeError(f"Tencent ex-rights date does not match for {symbol}")
    if Decimal(str(details.get("accepted_previous_close"))) != accepted_previous_close:
        raise RuntimeError(f"Tencent accepted previous close does not match for {symbol}")
    if Decimal(str(details.get("derived_previous_close"))) != reported_previous_close:
        raise RuntimeError(f"Tencent derived previous close does not match for {symbol}")

    if corporate_action_record is not None:
        record_symbol = str(corporate_action_record.get("symbol", ""))
        if record_symbol != symbol:
            raise RuntimeError(
                f"Eastmoney corporate-action symbol does not match for {symbol}: {record_symbol!r}"
            )
        if parse_date(corporate_action_record.get("ex_dividend_date")) != target_session:
            raise RuntimeError(f"Eastmoney ex-dividend date does not match for {symbol}")
        if parse_date(corporate_action_record.get("equity_record_date")) != previous_session:
            raise RuntimeError(f"Eastmoney equity-record date does not match for {symbol}")
        cash_value = corporate_action_record.get("cash_per_ten_shares")
        if cash_value is None or Decimal(str(cash_value)) <= 0:
            raise RuntimeError(f"Eastmoney pure-cash dividend is missing or nonpositive for {symbol}")
        cash_per_ten = Decimal(str(cash_value))
        for field in ("bonus_ratio", "conversion_ratio"):
            value = corporate_action_record.get(field)
            if value is not None and Decimal(str(value)) != 0:
                raise RuntimeError(
                    f"Eastmoney corporate action is not pure cash for {symbol}: {field}={value}"
                )
        tencent_cash = Decimal(str(details.get("cash_per_ten_shares")))
        if tencent_cash != cash_per_ten:
            raise RuntimeError(
                f"Eastmoney/Tencent cash dividend disagrees for {symbol}: "
                f"eastmoney={cash_per_ten} tencent={tencent_cash}"
            )
        canonical_record = dict(corporate_action_record)
        details["eastmoney_inventory_record"] = canonical_record
        details["eastmoney_inventory_record_sha256"] = sha256(canonical_record)

    return canonical_lineage_evidence({
        "symbol": symbol,
        "target_session": target_session.isoformat(),
        "kind": "cash_dividend_reference",
        "source": "tencent_archive",
        "details": details,
    })


def capture_symbol(
    *,
    plan: DailyIncrementalPlan,
    symbol: str,
    primary_source: AkshareEastmoneyHistorySource,
    verification_source: AkshareHistorySource,
    secondary_source: BaostockHistorySource | None,
    verification_fallback_source: BaostockHistorySource | None = None,
    fallback_suspended_symbols: frozenset[str] = frozenset(),
    fallback_status_available: bool = True,
    previous_states: dict[str, PreviousAdjustedState],
    fallback_previous_states: dict[str, PreviousAdjustedState] | None = None,
    ipo_dates: dict[str, date],
    calendar_dates: tuple[date, ...],
    corporate_action_record: Mapping[str, Any] | None = None,
) -> tuple[DailyEvidence, Decimal | None, str, Exception | None]:
    """Capture one symbol and classify its resumable checkpoint state."""
    target = plan.target_session
    verification_required = symbol in set(plan.verification_symbols)
    ipo_date = ipo_dates.get(symbol)
    if ipo_date is None:
        raise RuntimeError(f"base security reference missing for {symbol}")

    primary: DailyBar | None = None
    adjusted: HistoricalBar | None = None
    verification: DailyBar | None = None
    events: list[AdjustmentEvent] = []
    lineage_evidence: list[dict[str, Any]] = []
    recoverable_error: Exception | None = None
    primary_source_name: str | None = None
    reported: Decimal | None = None
    status_source: str | None = None
    previous_close_source: str | None = None
    secondary: dict[str, str] | None = None

    if secondary_source is not None:
        status_rows = secondary_source.fetch_status(symbol, target, target)
        secondary = status_rows.get(target)
        if secondary is None:
            raise RuntimeError(f"secondary status missing for {symbol}:{target.isoformat()}")
        trade_status = str(secondary.get("tradestatus", "")).strip()
        if trade_status not in {"0", "1"}:
            raise RuntimeError(f"secondary trade status is unknown for {symbol}:{target.isoformat()}")
        reported = Decimal(str(secondary["preclose"])) if str(secondary.get("preclose", "")).strip() else None
        status_source = "baostock_daily_status"
        previous_close_source = "baostock_reported_preclose" if reported is not None else None
    elif fallback_status_available:
        trade_status = "0" if symbol in fallback_suspended_symbols else "1"
        secondary = {"tradestatus": trade_status, "isST": "", "preclose": ""}
        status_source = EastmoneySuspensionSource.name
    else:
        trade_status = "1"

    if trade_status == "1":
        try:
            if secondary_source is None:
                raw_map, primary_source_name, reported, previous_close_source = (
                    primary_source.fetch_daily_raw_with_reference(
                        symbol, plan.previous_session, target,
                    )
                )
                if secondary is not None:
                    secondary["preclose"] = format(reported, "f")
            else:
                raw_map, primary_source_name = primary_source.fetch_raw_with_fallback(symbol, target, target)
            primary = _one_target_row(
                (row for row in raw_map.values() if row.business_date == target),
                symbol,
                target,
                "primary source",
            )
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
                status_source=status_source, previous_close_source=previous_close_source,
            ), reported, "succeeded", None
        error = recoverable_error or RuntimeError(f"active primary bar missing for {symbol}")
        return _checkpoint_evidence(
            primary=None, fact=fact, verification=None, adjusted=None, events=[],
            status_source=status_source, previous_close_source=previous_close_source,
        ), reported, "blocked", error

    state = previous_states.get(symbol)
    if state is None or state.business_date != plan.previous_session:
        prior_state = (fallback_previous_states or {}).get(symbol)
        recovery_error: Exception | None = None
        if prior_state is not None and prior_state.business_date < plan.previous_session:
            required_sessions = tuple(
                session for session in calendar_dates
                if prior_state.business_date <= session <= plan.previous_session
            )
            try:
                recovered_close, recovery_details = TencentHistorySource(
                    timeout_seconds=primary_source.timeout_seconds,
                    attempts=min(primary_source.attempts, 2),
                ).recover_no_adjustment_predecessor(
                    symbol,
                    prior_state.business_date,
                    plan.previous_session,
                    prior_state.raw_close,
                    required_sessions,
                )
                recovery_details.update({
                    "prior_source_dataset_id": prior_state.source_dataset_id,
                    "qfq_factor": format(prior_state.qfq_factor, "f"),
                    "hfq_factor": format(prior_state.hfq_factor, "f"),
                })
                recovery_evidence = canonical_lineage_evidence({
                    "symbol": symbol,
                    "target_session": target.isoformat(),
                    "kind": "gap_no_adjustment_recovery",
                    "source": "tencent_raw_hfq_continuity",
                    "details": recovery_details,
                })
                lineage_evidence.append(recovery_evidence)
                state = PreviousAdjustedState(
                    symbol=symbol,
                    business_date=plan.previous_session,
                    raw_close=recovered_close,
                    qfq_factor=prior_state.qfq_factor,
                    hfq_factor=prior_state.hfq_factor,
                    source_dataset_id=f"daily-lineage:{sha256(recovery_evidence)}",
                )
                previous_states[symbol] = state
            except Exception as error:
                recovery_error = error
        if state is None or state.business_date != plan.previous_session:
            error = recovery_error or RuntimeError(f"exact predecessor adjusted state missing for {symbol}")
        else:
            error = None
    else:
        error = None
    if error is not None:
        blocked_fact = derive_tradeability(
            symbol=symbol, business_date=target, index_code=plan.membership[symbol],
            listing_age_sessions=age, primary=_tradeability_row(primary), secondary=secondary,
        )
        blocked_fact = replace(
            blocked_fact,
            can_buy=False,
            can_sell=False,
            block_reasons=tuple(sorted(set(blocked_fact.block_reasons) | {"missing_adjustment_predecessor"})),
        )
        return _checkpoint_evidence(
            primary=primary, fact=blocked_fact, verification=None, adjusted=None, events=[],
            status_source=status_source, previous_close_source=previous_close_source,
            lineage_evidence=lineage_evidence,
        ), reported, "blocked", error
    if reported is None or reported <= 0:
        raise RuntimeError(f"positive reported previous close missing for {symbol}")
    try:
        corporate_action_candidate = symbol in set(plan.corporate_action_symbols)
        structured_cash_reference_captured = False
        if corporate_action_candidate or has_price_break(state.raw_close, reported) or previous_close_source in {
            "akshare_sina_exact_predecessor_close",
            "tencent_exact_predecessor_close",
        }:
            try:
                events = _target_events(primary_source, symbol, target)
            except SinaFactorsUnavailableError as factor_error:
                missing_sina_factors = True
                if corporate_action_candidate and missing_sina_factors:
                    if corporate_action_record is None:
                        raise RuntimeError(
                            f"corporate-action inventory record missing for {symbol}"
                        ) from factor_error
                    reported, action_details = TencentHistorySource(
                        timeout_seconds=primary_source.timeout_seconds,
                        attempts=min(primary_source.attempts, 2),
                    ).fetch_cash_dividend_reference(
                        symbol, plan.previous_session, target, state.raw_close,
                    )
                    lineage_evidence.append(_verified_cash_dividend_lineage(
                        symbol=symbol,
                        previous_session=plan.previous_session,
                        target_session=target,
                        accepted_previous_close=state.raw_close,
                        reported_previous_close=reported,
                        action_details=action_details,
                        corporate_action_record=corporate_action_record,
                    ))
                    events = [AdjustmentEvent(
                        symbol=symbol,
                        effective_date=target,
                        qfq_factor=state.qfq_factor,
                        hfq_factor=state.hfq_factor,
                        source=RQALPHA_DEFERRED_CASH_ACTION_SOURCE,
                    )]
                    previous_close_source = "tencent_structured_cash_dividend"
                    structured_cash_reference_captured = True
                    if secondary is not None:
                        secondary["preclose"] = format(reported, "f")
                elif (
                    corporate_action_candidate
                    or primary_source_name not in {"akshare_sina", "tencent_archive"}
                    or not missing_sina_factors
                ):
                    raise
                else:
                    continuity_source = TencentHistorySource(
                        timeout_seconds=primary_source.timeout_seconds,
                        attempts=min(primary_source.attempts, 2),
                    ).verify_no_adjustment_continuity(symbol, plan.previous_session, target)
                    previous_close_source = f"{previous_close_source}+{continuity_source}"
            if corporate_action_candidate and not events:
                raise RuntimeError(f"corporate-action candidate has no target factor event for {symbol}")
            if events and (
                corporate_action_candidate
                or previous_close_source in {
                    "akshare_sina_exact_predecessor_close",
                    "tencent_exact_predecessor_close",
                }
            ) and not structured_cash_reference_captured:
                reported, action_details = TencentHistorySource(
                    timeout_seconds=primary_source.timeout_seconds,
                    attempts=min(primary_source.attempts, 2),
                ).fetch_cash_dividend_reference(
                    symbol, plan.previous_session, target, state.raw_close,
                )
                previous_close_source = "tencent_structured_cash_dividend"
                lineage_evidence.append(_verified_cash_dividend_lineage(
                    symbol=symbol,
                    previous_session=plan.previous_session,
                    target_session=target,
                    accepted_previous_close=state.raw_close,
                    reported_previous_close=reported,
                    action_details=action_details,
                    corporate_action_record=(
                        corporate_action_record if corporate_action_candidate else None
                    ),
                ))
                if secondary is not None:
                    secondary["preclose"] = format(reported, "f")
        factor_references = _factor_reference_closes(lineage_evidence)
        adjusted_rows = build_daily_adjusted_bars(
            target_session=target, previous_session=plan.previous_session,
            membership=plan.membership, primary_bars=[primary], previous_states={symbol: state},
            reported_previous_closes={symbol: reported}, adjustment_events=events,
            factor_reference_closes=factor_references,
        )
        adjusted = adjusted_rows[0]
    except Exception as error:
        blocked_fact = derive_tradeability(
            symbol=symbol, business_date=target, index_code=plan.membership[symbol],
            listing_age_sessions=age, primary=_tradeability_row(primary), secondary=secondary,
        )
        blocked_fact = replace(
            blocked_fact,
            can_buy=False,
            can_sell=False,
            block_reasons=tuple(sorted(set(blocked_fact.block_reasons) | {"invalid_adjustment_continuity"})),
        )
        return _checkpoint_evidence(
            primary=primary, fact=blocked_fact, verification=None, adjusted=None, events=[],
            status_source=status_source, previous_close_source=previous_close_source,
            lineage_evidence=lineage_evidence,
        ), reported, "blocked", error
    fact = derive_tradeability(
        symbol=symbol, business_date=target, index_code=plan.membership[symbol],
        listing_age_sessions=age, primary=_tradeability_row(primary), secondary=secondary,
    )
    if verification_required:
        try:
            values = verification_source.fetch_raw(
                symbol, target, target, exclude_sources={str(primary_source_name or primary.source)},
            )
            verification = _one_target_row(values, symbol, target, "verification source")
        except Exception as error:
            recoverable_error = error
            if verification_fallback_source is not None and primary.source != "baostock":
                try:
                    fallback_status = verification_fallback_source.fetch_status(
                        symbol, target, target,
                    )
                    fallback_bars = verification_fallback_source.bars_from_status(
                        symbol, fallback_status,
                    )
                    verification = _one_target_row(
                        fallback_bars.values(), symbol, target, "BaoStock verification source",
                    )
                    consistency_errors = cross_source_consistency_errors(primary, verification)
                    if consistency_errors:
                        raise RuntimeError(
                            f"BaoStock verification disagrees for {symbol}: "
                            f"{','.join(consistency_errors)}"
                        )
                    recoverable_error = None
                except Exception as fallback_error:
                    verification = None
                    recoverable_error = RuntimeError(
                        f"{error}; baostock_verification: "
                        f"{type(fallback_error).__name__}: {fallback_error}"
                    )

    evidence = _checkpoint_evidence(
        primary=primary, fact=fact, verification=verification, adjusted=adjusted, events=events,
        status_source=status_source, previous_close_source=previous_close_source,
        lineage_evidence=lineage_evidence,
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


def _accepted_replay_result(
    latest_accepted: date,
    accepted_dataset_id: str,
    requested: date | None,
    *,
    base_history_dataset_id: str,
) -> dict[str, Any] | None:
    """Return the existing immutable result for an exact accepted-date replay."""
    if (
        requested is None
        or requested != latest_accepted
        or accepted_dataset_id == base_history_dataset_id
    ):
        return None
    return {
        "event": "daily_accepted",
        "dataset_id": accepted_dataset_id,
        "target_session": latest_accepted.isoformat(),
        "accepted": True,
        "idempotent_replay": True,
        "authoritative": False,
        "simulation_orders_allowed": False,
    }


def _reusable_existing_keys(
    succeeded_symbols: Iterable[str],
    target_session: date,
    corporate_action_symbols: Iterable[str],
) -> tuple[tuple[str, date], ...]:
    """Keep action candidates out of checkpoint reuse for the current session."""
    candidates = set(corporate_action_symbols)
    return tuple(
        (symbol, target_session)
        for symbol in sorted(set(succeeded_symbols) - candidates)
    )


def _checkpoint_reuse_exclusions(
    corporate_action_symbols: Iterable[str], *, finalize_only: bool,
) -> tuple[str, ...]:
    """Refresh action candidates during capture, then reuse verified final checkpoints."""
    if finalize_only:
        return ()
    return tuple(sorted(set(corporate_action_symbols)))


def run(
    *,
    observed_at: datetime,
    base_history_dataset_id: str,
    output_dir: Path,
    requested_target: date | None = None,
    initialize_schema: bool = False,
    symbol_attempts: int = 2,
    shard_index: int = 0,
    shard_count: int = 1,
    defer_finalize: bool = False,
    finalize_only: bool = False,
    supersedes_dataset_id: str | None = None,
) -> dict[str, Any]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if symbol_attempts < 1 or symbol_attempts > 3:
        raise ValueError("symbol attempts must be between 1 and 3")
    if shard_count < 1 or shard_count > 4 or not 0 <= shard_index < shard_count:
        raise ValueError("daily shard coordinates must use between 1 and 4 stable shards")
    if defer_finalize and finalize_only:
        raise ValueError("daily capture cannot defer and finalize in the same invocation")
    if supersedes_dataset_id is not None and requested_target is None:
        raise ValueError("daily correction requires an explicit target session")
    observed = observed_at.astimezone(SHANGHAI)
    _progress(
        "daily_prerequisites_started",
        phase="calendars",
        observed_date=observed.date().isoformat(),
        deadline_seconds=CALENDAR_DEADLINE_SECONDS,
    )
    with prerequisite_deadline(CALENDAR_DEADLINE_SECONDS):
        primary_calendar, secondary_calendar, _calendar_gates, _sources = load_calendars(observed.date())
    _progress(
        "daily_calendars_loaded",
        primary_sessions=len(primary_calendar.open_dates),
        secondary_sessions=len(secondary_calendar.open_dates),
    )
    config = TiDBConfig.from_env()
    connection = connect(config)
    try:
        if initialize_schema:
            ensure_daily_schema(connection)
        if supersedes_dataset_id is None:
            latest_accepted, predecessor_dataset_id = latest_accepted_lineage(
                connection, base_history_dataset_id,
            )
        else:
            latest_accepted, predecessor_dataset_id = daily_correction_context(
                connection,
                base_history_dataset_id=base_history_dataset_id,
                superseded_dataset_id=supersedes_dataset_id,
                target_session=requested_target,
            )
        latest_primary_ready = latest_closed_session(primary_calendar, observed)
        latest_secondary_ready = latest_closed_session(secondary_calendar, observed)
    finally:
        connection.close()

    replay = None
    if supersedes_dataset_id is None:
        replay = _accepted_replay_result(
            latest_accepted,
            predecessor_dataset_id,
            requested_target,
            base_history_dataset_id=base_history_dataset_id,
        )
    if replay is not None:
        _progress_result(replay)
        return replay

    discovery_calendar = tuple(sorted(
        set(primary_calendar.open_dates) | set(secondary_calendar.open_dates)
    ))
    latest_ready = max(latest_primary_ready, latest_secondary_ready)
    target = _select_target(discovery_calendar, latest_accepted, latest_ready, requested_target)
    if target is None:
        boundary = validate_daily_calendar_boundary(
            primary_calendar, secondary_calendar, latest_accepted,
        )
        result = {
            "event": "daily_noop", "latest_accepted_session": latest_accepted.isoformat(),
            "latest_ready_session": latest_ready.isoformat(), "simulation_orders_allowed": False,
            "calendar_diagnostics": boundary,
        }
        _progress(**result)
        return result

    if target > latest_primary_ready or target > latest_secondary_ready:
        raise RuntimeError(
            f"daily target {target.isoformat()} is beyond a closed calendar horizon: "
            f"primary={latest_primary_ready.isoformat()} "
            f"secondary={latest_secondary_ready.isoformat()}"
        )
    calendar_boundary = validate_daily_calendar_boundary(
        primary_calendar, secondary_calendar, target,
    )
    _progress(
        "daily_calendar_boundary_accepted",
        latest_primary_ready_session=latest_primary_ready.isoformat(),
        latest_secondary_ready_session=latest_secondary_ready.isoformat(),
        **calendar_boundary,
    )

    _progress("daily_prerequisites_started", phase="corporate_action_inventory")
    with prerequisite_deadline(CALENDAR_DEADLINE_SECONDS):
        corporate_action_inventory = EastmoneyCorporateActionSource(
            attempts=3, timeout_seconds=25,
        ).fetch(target)
    corporate_action_rows = list(corporate_action_inventory.records)
    _progress(
        "daily_corporate_action_inventory_loaded",
        source=corporate_action_inventory.source,
        target_session=target.isoformat(),
        record_count=len(corporate_action_rows),
        records_sha256=corporate_action_inventory.evidence_sha256,
    )

    _progress("daily_prerequisites_started", phase="point_in_time_universe")
    csi = CsiIndexSource()
    current = csi.fetch_current()
    events, discovered, _event_source = csi.fetch_indexed_events(current.as_of_date)
    snapshots = reconstruct(current, events)
    _progress(
        "daily_universe_loaded",
        current_as_of_date=current.as_of_date.isoformat(),
        event_count=len(events),
        discovered_notice_count=len(discovered),
    )
    base_plan = build_incremental_plan(
        observed_at=observed, primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar, snapshots=snapshots, target_session=target,
        corporate_action_inventory=corporate_action_rows,
        corporate_action_inventory_source=corporate_action_inventory.source,
    )
    dataset_id = default_daily_dataset_id(target, base_plan.scope_sha256)

    connection = connect(config)
    try:
        if initialize_schema:
            ensure_daily_schema(connection)
        stored, metadata = load_daily_checkpoint_evidence(connection, dataset_id)
        previous_states = load_previous_adjusted_states(
            connection, predecessor_dataset_id=predecessor_dataset_id,
            previous_session=base_plan.previous_session,
        )
        recovered_states = recovered_previous_states_from_lineage(
            stored.lineage_evidence,
            previous_session=base_plan.previous_session,
        )
        for symbol, recovered_state in recovered_states.items():
            existing_state = previous_states.get(symbol)
            if existing_state is not None and existing_state != recovered_state:
                raise RuntimeError(f"stored lineage conflicts with accepted predecessor for {symbol}")
            previous_states[symbol] = recovered_state
        missing_predecessor_symbols = set(base_plan.membership) - set(previous_states)
        fallback_previous_states = load_latest_prior_adjusted_states(
            connection,
            base_history_dataset_id=base_history_dataset_id,
            previous_session=base_plan.previous_session,
            symbols=missing_predecessor_symbols,
        )
        ipo_dates = load_base_references(connection, base_history_dataset_id)
        recovery = {
            "already_present": len(metadata["succeeded_symbols"]),
            "recovered": 0,
            "candidate_datasets": 0,
            "recovered_by_source_dataset": {},
            "rejected_datasets": {},
        }
        if len(metadata["succeeded_symbols"]) < len(base_plan.expected_membership):
            recovery = recover_compatible_daily_checkpoints(
                connection,
                dataset_id=dataset_id,
                target_session=target,
                expected_membership=base_plan.membership,
                verification_symbols=base_plan.verification_symbols,
                previous_states=previous_states,
                existing_metadata=metadata,
                excluded_symbols=base_plan.corporate_action_symbols,
            )
            if recovery["recovered"]:
                stored, metadata = load_daily_checkpoint_evidence(connection, dataset_id)
        plan = build_incremental_plan(
            observed_at=observed, primary_calendar=primary_calendar,
            secondary_calendar=secondary_calendar, snapshots=snapshots,
            accepted_existing_keys=_reusable_existing_keys(
                metadata["succeeded_symbols"], target,
                _checkpoint_reuse_exclusions(
                    base_plan.corporate_action_symbols,
                    finalize_only=finalize_only,
                ),
            ),
            target_session=target,
            corporate_action_inventory=corporate_action_rows,
            corporate_action_inventory_source=corporate_action_inventory.source,
        )
    finally:
        connection.close()

    _progress(
        "daily_scope_ready", dataset_id=dataset_id, target_session=target.isoformat(),
        predecessor_dataset_id=predecessor_dataset_id,
        expected_symbols=len(plan.expected_membership), resumed_symbols=len(plan.accepted_existing_symbols),
        fetch_symbols=len(plan.fetch_symbols), verification_symbols=len(plan.verification_symbols),
        recovered_symbols=recovery["recovered"],
        recovery_candidate_datasets=recovery["candidate_datasets"],
        recovery_rejected_datasets=recovery["rejected_datasets"],
        corporate_action_candidates=len(plan.corporate_action_symbols),
    )

    all_scope_symbols = list(daily_membership_symbols(plan.expected_membership))
    corporate_action_by_symbol = {
        str(record["symbol"]): record for record in corporate_action_rows
    }
    assigned_symbols = set(all_scope_symbols[shard_index::shard_count])
    selected_fetch_symbols = list(select_daily_shard_symbols(
        all_scope_symbols, plan.fetch_symbols, shard_index, shard_count,
    ))
    if finalize_only and plan.fetch_symbols:
        blocked_result = {
            "event": "daily_blocked",
            "dataset_id": dataset_id,
            "target_session": target.isoformat(),
            "accepted": False,
            "authoritative": False,
            "simulation_orders_allowed": False,
            "remaining_symbols": len(plan.fetch_symbols),
            "blocked_symbols": sorted(plan.fetch_symbols),
            "reason": "symbol checkpoints incomplete; see TiDB checkpoint error details",
        }
        _progress_result(blocked_result)
        return blocked_result
    if finalize_only:
        selected_fetch_symbols = []
    _progress(
        "daily_shard_scope_ready", shard_index=shard_index, shard_count=shard_count,
        assigned_symbols=len(assigned_symbols), pending_symbols=len(selected_fetch_symbols),
        finalize_only=finalize_only,
    )

    primary_source = AkshareEastmoneyHistorySource(timeout_seconds=25, attempts=2)
    verification_source = AkshareHistorySource(timeout_seconds=25, attempts=2)
    with ExitStack() as source_stack:
        secondary_source: BaostockHistorySource | None = None
        verification_fallback_source: BaostockHistorySource | None = None
        fallback_suspended_symbols: frozenset[str] = frozenset()
        fallback_status_available = False
        if selected_fetch_symbols:
            try:
                fallback_suspended_symbols = EastmoneySuspensionSource(
                    attempts=3, timeout_seconds=25,
                ).fetch(target)
                fallback_status_available = True
                _progress(
                    "daily_status_source_ready",
                    source=EastmoneySuspensionSource.name,
                    confirmed_suspended=len(fallback_suspended_symbols),
                    st_policy="unknown_fail_closed",
                )
            except Exception as error:
                _progress(
                    "daily_status_source_degraded",
                    unavailable_source=EastmoneySuspensionSource.name,
                    error=f"{type(error).__name__}: {error}",
                    fallback_source="baostock_daily_status",
                )
                try:
                    secondary_source = source_stack.enter_context(
                        BaostockHistorySource(timeout_seconds=25, attempts=2)
                    )
                    _progress(
                        "daily_status_source_ready",
                        source="baostock_daily_status",
                    )
                except Exception as fallback_error:
                    _progress(
                        "daily_status_source_unavailable",
                        unavailable_source="baostock_daily_status",
                        error=f"{type(fallback_error).__name__}: {fallback_error}",
                        trading_policy="missing_status_blocks_buy_and_sell",
                    )
        verification_pending = bool(
            set(selected_fetch_symbols) & set(plan.verification_symbols)
        )
        if verification_pending:
            verification_fallback_source = secondary_source
            if verification_fallback_source is None:
                try:
                    verification_fallback_source = source_stack.enter_context(
                        BaostockHistorySource(timeout_seconds=25, attempts=1)
                    )
                    _progress(
                        "daily_verification_fallback_ready",
                        source="baostock_daily_bar",
                        policy="used_only_after_non_primary_public_sources_fail",
                    )
                except Exception as fallback_error:
                    _progress(
                        "daily_verification_fallback_unavailable",
                        unavailable_source="baostock_daily_bar",
                        error=f"{type(fallback_error).__name__}: {fallback_error}",
                        verification_policy="missing_verification_fails_closed",
                    )
        for position, symbol in enumerate(selected_fetch_symbols, start=1):
            final_error: Exception | str | None = None
            for attempt in range(1, symbol_attempts + 1):
                try:
                    with symbol_deadline(SYMBOL_DEADLINE_SECONDS):
                        evidence, reported, status, error = capture_symbol(
                            plan=plan, symbol=symbol, primary_source=primary_source,
                            verification_source=verification_source, secondary_source=secondary_source,
                            verification_fallback_source=verification_fallback_source,
                            fallback_suspended_symbols=fallback_suspended_symbols,
                            fallback_status_available=fallback_status_available,
                            previous_states=previous_states,
                            fallback_previous_states=fallback_previous_states,
                            ipo_dates=ipo_dates,
                            calendar_dates=primary_calendar.open_dates,
                            corporate_action_record=corporate_action_by_symbol.get(symbol),
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
                            status_source=(
                                "baostock_daily_status" if secondary_source is not None
                                else EastmoneySuspensionSource.name if fallback_status_available
                                else None
                            ),
                        ),
                        dataset_id=dataset_id, symbol=symbol, target_session=target,
                        verification_required=symbol in set(plan.verification_symbols),
                        reported_previous_close=None, status="failed", error=final_error or "unknown failure",
                    )
                finally:
                    failed_connection.close()
            _progress(
                "daily_symbol_completed", symbol=symbol, completed=position,
                total=len(selected_fetch_symbols), shard_index=shard_index,
            )

    if defer_finalize:
        result = {
            "event": "daily_shard_capture_completed", "dataset_id": dataset_id,
            "target_session": target.isoformat(), "shard_index": shard_index,
            "shard_count": shard_count, "attempted_symbols": len(selected_fetch_symbols),
            "simulation_orders_allowed": False,
        }
        _progress(**result)
        return result

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
    lineage_rows = sorted(
        (canonical_lineage_evidence(row) for row in stored.lineage_evidence),
        key=lambda row: (row["symbol"], row["kind"]),
    )
    reported_closes = metadata["reported_previous_closes"]
    factor_references = _factor_reference_closes(lineage_rows)
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
        factor_reference_closes=factor_references,
        lineage_evidence=lineage_rows,
        primary_failures=primary_failures, verification_failures=verification_failures,
    )
    status_source_counts: dict[str, int] = {}
    for source in metadata["status_sources"].values():
        status_source_counts[source] = status_source_counts.get(source, 0) + 1
    previous_close_source_counts: dict[str, int] = {}
    for source in metadata["reported_previous_close_sources"].values():
        previous_close_source_counts[source] = previous_close_source_counts.get(source, 0) + 1
    manifest["status_source_counts"] = dict(sorted(status_source_counts.items()))
    manifest["reported_previous_close_source_counts"] = dict(sorted(previous_close_source_counts.items()))
    manifest["recovered_checkpoint_count"] = len(metadata["checkpoint_origin_dataset_ids"])
    manifest["lineage_evidence_count"] = len(lineage_rows)
    manifest["lineage_evidence_sha256"] = sha256(lineage_rows)
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
    if supersedes_dataset_id is not None:
        manifest.update({
            "supersedes_dataset_id": supersedes_dataset_id,
            "correction_reason": "corporate_action_inventory_false_green",
        })
    write_outputs(
        output_dir, manifest, primary_rows, fact_rows, verification_rows,
        adjusted_rows, event_rows, lineage_rows, corporate_action_rows,
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
        lineage_evidence=lineage_rows,
    )
    connection = connect(config)
    try:
        result = publish_daily_run(
            connection, publication, dataset_id=dataset_id,
            base_history_dataset_id=base_history_dataset_id,
            predecessor_dataset_id=predecessor_dataset_id,
            supersedes_dataset_id=supersedes_dataset_id,
            correction_reason=(
                "corporate_action_inventory_false_green"
                if supersedes_dataset_id is not None else None
            ),
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
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--defer-finalize", action="store_true")
    parser.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--supersedes-dataset-id")
    args = parser.parse_args()
    observed_at = args.observed_at or datetime.now(SHANGHAI)
    result = run(
        observed_at=observed_at, base_history_dataset_id=args.base_history_dataset_id,
        output_dir=args.output_dir, requested_target=args.target_session,
        initialize_schema=args.init_schema, symbol_attempts=args.symbol_attempts,
        shard_index=args.shard_index, shard_count=args.shard_count,
        defer_finalize=args.defer_finalize, finalize_only=args.finalize_only,
        supersedes_dataset_id=args.supersedes_dataset_id,
    )
    return 0 if result.get(
        "accepted", result.get("event") in {"daily_noop", "daily_shard_capture_completed"}
    ) else 2


if __name__ == "__main__":
    sys.exit(main())

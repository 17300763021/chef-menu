"""Fail-closed quality gates for a single-session M2 daily increment."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping

from scripts.market_data.contracts import DailyBar
from scripts.market_data.quality_gates import GateResult
from scripts.market_data.tradeability_contracts import TradeabilityFact, limit_rate, rounded_limit


CLOSE_TOLERANCE_RATE = Decimal("0.0005")


def _limited(values: Iterable[str], maximum: int = 20) -> tuple[str, ...]:
    return tuple(sorted(values)[:maximum])


def _price_tolerance(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), abs(value) * CLOSE_TOLERANCE_RATE)


def _valid_positive_price(value: Decimal | None) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def cross_source_consistency_errors(primary: DailyBar, verification: DailyBar) -> tuple[str, ...]:
    """Return exact per-symbol reasons that make a verification pair unsafe to reuse."""
    errors: list[str] = []
    if primary.key != verification.key:
        errors.append("key")
    if primary.source == verification.source:
        errors.append("source_independence")
    if abs(primary.close - verification.close) > _price_tolerance(primary.close):
        errors.append("close")
    volume_tolerance = max(100, int(verification.volume_shares * 0.001))
    if abs(primary.volume_shares - verification.volume_shares) > volume_tolerance:
        errors.append("volume")
    amount_tolerance = max(Decimal("1.00"), abs(verification.amount_cny) * Decimal("0.0001"))
    if abs(primary.amount_cny - verification.amount_cny) > amount_tolerance:
        errors.append("amount")
    return tuple(errors)


def evaluate_daily_incremental(
    *,
    target_session: date,
    previous_session: date,
    expected_membership: Mapping[str, str],
    primary_bars: Iterable[DailyBar],
    tradeability_facts: Iterable[TradeabilityFact],
    verification_bars: Iterable[DailyBar],
    verification_symbols: Iterable[str],
    accepted_previous_closes: Mapping[str, Decimal],
    reported_previous_closes: Mapping[str, Decimal],
    primary_failures: Mapping[str, str] | None = None,
    verification_failures: Mapping[str, str] | None = None,
) -> list[GateResult]:
    """Evaluate one complete target-session package without weakening missing-data gates."""
    expected = dict(expected_membership)
    expected_symbols = set(expected)
    primary_rows = list(primary_bars)
    fact_rows = list(tradeability_facts)
    verification_rows = list(verification_bars)
    requested_verification = set(verification_symbols)
    primary_errors = dict(primary_failures or {})
    verification_errors = dict(verification_failures or {})
    results: list[GateResult] = [
        GateResult("daily_expected_universe_nonempty", bool(expected), len(expected), "> 0"),
        GateResult(
            "daily_session_order",
            previous_session < target_session,
            f"{previous_session.isoformat()} -> {target_session.isoformat()}",
            "previous_session < target_session",
        ),
        GateResult(
            "daily_primary_fetch_failures",
            not primary_errors,
            len(primary_errors),
            "= 0",
            critical=False,
            details=_limited(f"{symbol}: {message}" for symbol, message in primary_errors.items()),
        ),
        GateResult(
            "daily_verification_fetch_failures",
            not verification_errors,
            len(verification_errors),
            "= 0",
            critical=False,
            details=_limited(f"{symbol}: {message}" for symbol, message in verification_errors.items()),
        ),
    ]

    primary_key_counts = Counter(row.key for row in primary_rows)
    duplicate_primary = [
        f"{symbol}:{business_date.isoformat()}"
        for (symbol, business_date), count in primary_key_counts.items()
        if count > 1
    ]
    primary_scope_errors = [
        f"{row.symbol}:{row.business_date.isoformat()}"
        for row in primary_rows
        if row.business_date != target_session or row.symbol not in expected_symbols
    ]
    non_raw_primary = [
        f"{row.symbol}:{row.adjustment}"
        for row in primary_rows
        if row.adjustment != "none"
    ]
    invalid_ohlc = [
        f"{row.symbol}:{row.business_date.isoformat()}"
        for row in primary_rows
        if min(row.open, row.high, row.low, row.close) <= 0
        or row.low > min(row.open, row.close)
        or row.high < max(row.open, row.close)
        or row.low > row.high
    ]
    invalid_units = [
        f"{row.symbol}:{row.business_date.isoformat()}"
        for row in primary_rows
        if row.volume_shares < 0 or row.amount_cny < 0
    ]
    results.extend([
        GateResult("daily_primary_duplicate_keys", not duplicate_primary, len(duplicate_primary), "= 0", details=_limited(duplicate_primary)),
        GateResult("daily_primary_scope", not primary_scope_errors, len(primary_scope_errors), "= 0 target-session universe violations", details=_limited(primary_scope_errors)),
        GateResult("daily_primary_unadjusted", not non_raw_primary, len(non_raw_primary), "= 0 adjusted rows", details=_limited(non_raw_primary)),
        GateResult("daily_primary_ohlc", not invalid_ohlc, len(invalid_ohlc), "= 0", details=_limited(invalid_ohlc)),
        GateResult("daily_primary_nonnegative_units", not invalid_units, len(invalid_units), "= 0", details=_limited(invalid_units)),
    ])

    fact_key_counts = Counter((row.symbol, row.business_date) for row in fact_rows)
    duplicate_facts = [
        f"{symbol}:{business_date.isoformat()}"
        for (symbol, business_date), count in fact_key_counts.items()
        if count > 1
    ]
    fact_scope_errors = [
        f"{row.symbol}:{row.business_date.isoformat()}"
        for row in fact_rows
        if row.business_date != target_session or row.symbol not in expected_symbols
    ]
    fact_map = {
        row.symbol: row
        for row in fact_rows
        if row.business_date == target_session and row.symbol in expected_symbols
    }
    fact_coverage = len(set(fact_map) & expected_symbols) * 10000 // max(1, len(expected_symbols))
    missing_facts = expected_symbols - set(fact_map)
    index_mismatches = [
        f"{symbol}:{fact_map[symbol].index_code}:{expected[symbol]}"
        for symbol in sorted(set(fact_map) & expected_symbols)
        if fact_map[symbol].index_code != expected[symbol]
    ]
    invalid_listing_ages = [
        f"{row.symbol}:{row.listing_age_sessions}"
        for row in fact_rows
        if row.listing_age_sessions < 0
    ]
    results.extend([
        GateResult("daily_tradeability_duplicate_keys", not duplicate_facts, len(duplicate_facts), "= 0", details=_limited(duplicate_facts)),
        GateResult("daily_tradeability_scope", not fact_scope_errors, len(fact_scope_errors), "= 0 target-session universe violations", details=_limited(fact_scope_errors)),
        GateResult(
            "daily_tradeability_coverage",
            fact_coverage == 10000,
            f"{fact_coverage / 100:.2f}%",
            "= 100.00%",
            details=_limited(missing_facts),
        ),
        GateResult("daily_tradeability_index_alignment", not index_mismatches, len(index_mismatches), "= 0", details=_limited(index_mismatches)),
        GateResult("daily_tradeability_nonnegative_listing_age", not invalid_listing_ages, len(invalid_listing_ages), "= 0", details=_limited(invalid_listing_ages)),
    ])

    primary_map = {
        row.symbol: row
        for row in primary_rows
        if row.business_date == target_session and row.symbol in expected_symbols
    }
    confirmed_suspended = {
        symbol
        for symbol, fact in fact_map.items()
        if fact.has_secondary_status and fact.is_suspended
    }
    active_expected = expected_symbols - confirmed_suspended
    observed_active = set(primary_map) & active_expected
    active_coverage = len(observed_active) * 10000 // max(1, len(active_expected))
    missing_active = active_expected - set(primary_map)
    results.append(GateResult(
        "daily_active_bar_coverage",
        bool(active_expected) and active_coverage >= 9800,
        f"{len(observed_active)}/{len(active_expected)} ({active_coverage / 100:.2f}%)",
        ">= 98.00%, excluding confirmed suspensions only",
        details=_limited(missing_active),
    ))

    fact_bar_mismatches = [
        f"{symbol}:fact={fact_map[symbol].has_primary_bar}:observed={symbol in primary_map}"
        for symbol in sorted(set(fact_map) & expected_symbols)
        if fact_map[symbol].has_primary_bar != (symbol in primary_map)
    ]
    suspended_bar_conflicts = [
        symbol
        for symbol, fact in fact_map.items()
        if fact.is_suspended and symbol in primary_map
    ]
    unsafe_facts = [
        f"{fact.symbol}:{fact.business_date.isoformat()}"
        for fact in fact_rows
        if (
            fact.can_buy
            and (
                not fact.has_primary_bar
                or not fact.has_secondary_status
                or fact.is_suspended
                or fact.is_st is None
                or fact.one_price_limit_up
            )
        ) or (
            fact.can_sell
            and (
                not fact.has_primary_bar
                or not fact.has_secondary_status
                or fact.is_suspended
                or fact.is_st is None
                or fact.one_price_limit_down
            )
        )
    ]
    missing_block_reasons: list[str] = []
    for fact in fact_rows:
        required_reasons = {
            reason
            for condition, reason in (
                (not fact.has_primary_bar, "missing_primary_bar"),
                (not fact.has_secondary_status, "missing_secondary_status"),
                (fact.is_suspended, "suspended"),
                (fact.is_st is None, "unknown_st_status"),
                (fact.one_price_limit_up, "one_price_limit_up"),
                (fact.one_price_limit_down, "one_price_limit_down"),
            )
            if condition
        }
        missing = required_reasons - set(fact.block_reasons)
        if missing:
            missing_block_reasons.append(f"{fact.symbol}:{','.join(sorted(missing))}")
    results.extend([
        GateResult("daily_tradeability_bar_alignment", not fact_bar_mismatches, len(fact_bar_mismatches), "= 0", details=_limited(fact_bar_mismatches)),
        GateResult("daily_suspended_bar_conflicts", not suspended_bar_conflicts, len(suspended_bar_conflicts), "= 0", details=_limited(suspended_bar_conflicts)),
        GateResult("daily_tradeability_fail_closed", not unsafe_facts, len(unsafe_facts), "= 0", details=_limited(unsafe_facts)),
        GateResult("daily_tradeability_reason_completeness", not missing_block_reasons, len(missing_block_reasons), "= 0", details=_limited(missing_block_reasons)),
    ])

    previous_close_required = {
        symbol
        for symbol, fact in fact_map.items()
        if not fact.is_suspended and fact.has_primary_bar and fact.listing_age_sessions >= 5
    }
    missing_previous_close = [
        symbol
        for symbol in previous_close_required
        if symbol not in accepted_previous_closes or symbol not in reported_previous_closes
        or not _valid_positive_price(accepted_previous_closes[symbol])
        or not _valid_positive_price(reported_previous_closes[symbol])
    ]
    previous_close_mismatches = []
    for symbol in sorted(previous_close_required - set(missing_previous_close)):
        accepted_close = accepted_previous_closes[symbol]
        reported_close = reported_previous_closes[symbol]
        if abs(accepted_close - reported_close) > _price_tolerance(accepted_close):
            previous_close_mismatches.append(f"{symbol}:{accepted_close}:{reported_close}")
    price_limit_mismatches: list[str] = []
    price_limit_symbols = {
        symbol
        for symbol, fact in fact_map.items()
        if not fact.is_suspended and fact.has_primary_bar and fact.is_st is not None
    }
    for symbol in sorted(price_limit_symbols):
        fact = fact_map[symbol]
        assert fact.is_st is not None
        expected_rate = limit_rate(symbol, target_session, fact.is_st, fact.listing_age_sessions)
        if expected_rate is not None and symbol not in reported_previous_closes:
            continue
        reported_close = reported_previous_closes.get(symbol)
        if expected_rate is not None and not _valid_positive_price(reported_close):
            continue
        expected_up = rounded_limit(reported_close, expected_rate, 1) if expected_rate and reported_close else None
        expected_down = rounded_limit(reported_close, expected_rate, -1) if expected_rate and reported_close else None
        if fact.limit_rate != expected_rate or fact.limit_up != expected_up or fact.limit_down != expected_down:
            price_limit_mismatches.append(
                f"{symbol}:rate={fact.limit_rate}/{expected_rate}:up={fact.limit_up}/{expected_up}:down={fact.limit_down}/{expected_down}"
            )
    results.extend([
        GateResult(
            "daily_previous_close_coverage",
            not missing_previous_close,
            f"{len(previous_close_required) - len(missing_previous_close)}/{len(previous_close_required)}",
            "= 100.00% for seasoned active symbols",
            details=_limited(missing_previous_close),
        ),
        GateResult(
            "daily_previous_close_continuity",
            not previous_close_mismatches,
            len(previous_close_mismatches),
            "= 0 unless a separately verified adjustment event reconciles the break",
            critical=False,
            details=_limited(previous_close_mismatches),
        ),
        GateResult(
            "daily_price_limit_reconciliation",
            not price_limit_mismatches,
            len(price_limit_mismatches),
            "= 0",
            details=_limited(price_limit_mismatches),
        ),
    ])

    verification_key_counts = Counter(row.key for row in verification_rows)
    duplicate_verification = [
        f"{symbol}:{business_date.isoformat()}"
        for (symbol, business_date), count in verification_key_counts.items()
        if count > 1
    ]
    verification_scope_errors = [
        f"{row.symbol}:{row.business_date.isoformat()}"
        for row in verification_rows
        if row.business_date != target_session or row.symbol not in requested_verification
    ]
    verification_map = {
        row.symbol: row
        for row in verification_rows
        if row.business_date == target_session and row.symbol in requested_verification
    }
    verification_expected = requested_verification & observed_active
    verified = verification_expected & set(verification_map)
    verification_coverage = len(verified) * 10000 // max(1, len(verification_expected))
    missing_verification = verification_expected - set(verification_map)
    source_overlaps = [
        f"{symbol}:{primary_map[symbol].source}"
        for symbol in sorted(verified)
        if primary_map[symbol].source == verification_map[symbol].source
    ]
    close_mismatches = []
    volume_mismatches = []
    amount_mismatches = []
    for symbol in sorted(verified):
        pair_errors = cross_source_consistency_errors(primary_map[symbol], verification_map[symbol])
        first = primary_map[symbol].close
        second = verification_map[symbol].close
        if "close" in pair_errors:
            close_mismatches.append(f"{symbol}:{first}:{second}")
        first_volume = primary_map[symbol].volume_shares
        second_volume = verification_map[symbol].volume_shares
        if "volume" in pair_errors:
            volume_mismatches.append(f"{symbol}:{first_volume}:{second_volume}")
        first_amount = primary_map[symbol].amount_cny
        second_amount = verification_map[symbol].amount_cny
        if "amount" in pair_errors:
            amount_mismatches.append(f"{symbol}:{first_amount}:{second_amount}")
    consistency = (len(verified) - len(close_mismatches)) * 10000 // max(1, len(verified))
    volume_consistency = (len(verified) - len(volume_mismatches)) * 10000 // max(1, len(verified))
    amount_consistency = (len(verified) - len(amount_mismatches)) * 10000 // max(1, len(verified))
    results.extend([
        GateResult("daily_verification_duplicate_keys", not duplicate_verification, len(duplicate_verification), "= 0", details=_limited(duplicate_verification)),
        GateResult("daily_verification_scope", not verification_scope_errors, len(verification_scope_errors), "= 0", details=_limited(verification_scope_errors)),
        GateResult(
            "daily_cross_source_coverage",
            bool(verification_expected) and verification_coverage >= 9500,
            f"{len(verified)}/{len(verification_expected)} ({verification_coverage / 100:.2f}%)",
            ">= 95.00% of eligible preregistered targets",
            details=_limited(missing_verification),
        ),
        GateResult("daily_cross_source_independence", not source_overlaps, len(source_overlaps), "= 0 same-source pairs", details=_limited(source_overlaps)),
        GateResult(
            "daily_cross_source_close",
            bool(verified) and consistency >= 9950,
            f"{consistency / 100:.2f}%",
            ">= 99.50%",
            details=_limited(close_mismatches),
        ),
        GateResult(
            "daily_cross_source_volume_units",
            bool(verified) and volume_consistency >= 9950,
            f"{volume_consistency / 100:.2f}%",
            ">= 99.50%",
            details=_limited(volume_mismatches),
        ),
        GateResult(
            "daily_cross_source_amount",
            bool(verified) and amount_consistency >= 9950,
            f"{amount_consistency / 100:.2f}%",
            ">= 99.50%",
            details=_limited(amount_mismatches),
        ),
    ])
    return results

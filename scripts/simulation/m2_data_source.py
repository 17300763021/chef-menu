"""Admit immutable M2 daily evidence and validate RQAlpha corporate actions.

M2 owns raw market evidence and exact ex-date action facts.  RQAlpha owns
portfolio adjustment and dividend accounting.  This module deliberately does
not translate vendor QFQ/HFQ absolute factors into engine authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from scripts.market_data.manifest import sha256


PINNED_DAILY_V5_RELEASE_ID = (
    "m2-daily-2026-07-28-"
    "1392a4e46e59cd69fc330a36e81176070f18f27dde759a9376721411a3f7b851"
)
_SHA256_CHARS = frozenset("0123456789abcdef")


def _date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO business date") from error


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be positive and finite")
    return result


def _valid_sha256(value: Any) -> bool:
    text = str(value)
    return len(text) == 64 and all(character in _SHA256_CHARS for character in text)


def daily_release_id(target_session: date, scope_sha256: str) -> str:
    if not _valid_sha256(scope_sha256):
        raise ValueError("daily release id requires a lowercase SHA-256 scope")
    return f"m2-daily-{target_session.isoformat()}-{scope_sha256}"


class DailyEvidenceLike(Protocol):
    manifest: dict[str, Any]
    primary_bars: list[dict[str, Any]]
    tradeability: list[dict[str, Any]]
    lineage_evidence: list[dict[str, Any]]


@dataclass(frozen=True)
class M2AdmissionPolicy:
    """Explicit allow-list; callers must never select the latest release."""

    allowed_release_ids: frozenset[str]
    expected_symbol_count: int

    def __post_init__(self) -> None:
        if not self.allowed_release_ids or self.expected_symbol_count < 1:
            raise ValueError("M2 admission policy requires releases and a positive scope")


@dataclass(frozen=True)
class CashDividendExpectation:
    symbol: str
    book_closure_date: date
    ex_dividend_date: date
    cash_before_tax: Decimal
    round_lot: Decimal
    evidence_source: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if len(self.symbol) != 6 or not self.symbol.isdigit():
            raise ValueError("cash-dividend expectation requires a six-digit symbol")
        if not self.book_closure_date < self.ex_dividend_date:
            raise ValueError("cash-dividend registration must precede its ex-date")
        if self.cash_before_tax <= 0 or self.round_lot <= 0:
            raise ValueError("cash-dividend amount and round lot must be positive")
        if not self.evidence_source or not _valid_sha256(self.evidence_sha256):
            raise ValueError("cash-dividend expectation requires attributable hashed evidence")

    @property
    def order_book_id(self) -> str:
        from .rqalpha_adapter import rqalpha_order_book_id

        return rqalpha_order_book_id(self.symbol)


@dataclass(frozen=True)
class M2EngineInputRelease:
    release_id: str
    business_date: date
    base_history_dataset_id: str
    manifest_sha256: str
    primary_bars: tuple[Mapping[str, Any], ...]
    tradeability_facts: tuple[Mapping[str, Any], ...]
    cash_dividends: tuple[CashDividendExpectation, ...]

    def __post_init__(self) -> None:
        if not self.release_id or not self.base_history_dataset_id or not _valid_sha256(self.manifest_sha256):
            raise ValueError("M2 engine input requires immutable release lineage")
        symbols = self.symbols
        dividend_symbols = [row.symbol for row in self.cash_dividends]
        if len(dividend_symbols) != len(set(dividend_symbols)):
            raise ValueError("M2 engine input contains duplicate cash-dividend expectations")
        if not set(dividend_symbols).issubset(symbols):
            raise ValueError("M2 dividend evidence is outside the admitted symbol scope")

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(str(row["symbol"]) for row in self.tradeability_facts)

    @property
    def dividends_by_order_book_id(self) -> dict[str, CashDividendExpectation]:
        return {row.order_book_id: row for row in self.cash_dividends}


def _cash_dividends(
    lineage_rows: Iterable[Mapping[str, Any]],
    business_date: date,
) -> tuple[CashDividendExpectation, ...]:
    results: dict[str, CashDividendExpectation] = {}
    for raw in lineage_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("M2 lineage evidence must be structured")
        if str(raw.get("kind", "")) != "cash_dividend_reference":
            continue
        row = dict(raw)
        symbol = str(row.get("symbol", ""))
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("cash-dividend lineage requires a normalized symbol")
        if row.get("source") != "tencent_archive":
            raise ValueError("cash-dividend lineage requires its attributed M2 source")
        if _date(row["target_session"], "lineage target_session") != business_date:
            raise ValueError("cash-dividend lineage is outside the admitted release date")
        details = row.get("details")
        if not isinstance(details, Mapping):
            raise ValueError("cash-dividend lineage requires structured details")
        registration = _date(details.get("registration_date"), "registration_date")
        previous = _date(details.get("previous_session"), "previous_session")
        ex_date = _date(details.get("ex_rights_date"), "ex_rights_date")
        cash = _positive_decimal(details.get("cash_per_ten_shares"), "cash_per_ten_shares")
        accepted_close = _positive_decimal(details.get("accepted_previous_close"), "accepted_previous_close")
        derived_close = _positive_decimal(details.get("derived_previous_close"), "derived_previous_close")
        expected_close = (accepted_close - cash / Decimal("10")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if previous != registration or ex_date != business_date or not previous < business_date:
            raise ValueError("cash-dividend lineage dates do not reconcile")
        if derived_close != expected_close:
            raise ValueError("cash-dividend lineage arithmetic does not reconcile")
        if not str(details.get("action_content", "")).strip() or not _valid_sha256(
            details.get("vendor_action_sha256")
        ):
            raise ValueError("cash-dividend lineage lacks attributable action evidence")
        expectation = CashDividendExpectation(
            symbol=symbol,
            book_closure_date=registration,
            ex_dividend_date=ex_date,
            cash_before_tax=cash,
            round_lot=Decimal("10"),
            evidence_source=str(row["source"]),
            evidence_sha256=sha256(row),
        )
        existing = results.get(symbol)
        if existing is not None and existing != expectation:
            raise ValueError(f"conflicting cash-dividend evidence for {symbol}")
        results[symbol] = expectation
    return tuple(results[symbol] for symbol in sorted(results))


def admit_daily_evidence(
    evidence: DailyEvidenceLike,
    policy: M2AdmissionPolicy,
) -> M2EngineInputRelease:
    """Fail closed unless one explicitly pinned research release is intact."""

    manifest = evidence.manifest
    if manifest.get("accepted") is not True:
        raise ValueError("M2 engine input requires an accepted daily release")
    if manifest.get("authoritative") is not False or manifest.get("simulation_orders_allowed") is not False:
        raise ValueError("M2 input escaped its research-only boundary")
    if not str(manifest.get("schema_version", "")).startswith("m2-daily-incremental-v"):
        raise ValueError("unsupported M2 daily schema")
    business_date = _date(manifest.get("target_session"), "target_session")
    scope_sha256 = str(manifest.get("scope_sha256", ""))
    if not _valid_sha256(scope_sha256):
        raise ValueError("M2 daily release has an invalid scope hash")
    release_id = daily_release_id(business_date, scope_sha256)
    declared_release_id = str(manifest.get("dataset_id", release_id))
    if declared_release_id != release_id:
        raise ValueError("M2 manifest dataset id does not match its immutable scope")
    if release_id not in policy.allowed_release_ids:
        raise ValueError(f"M2 release is not explicitly admitted: {release_id}")

    evidence_checks = {
        "primary_row_count": len(evidence.primary_bars),
        "tradeability_row_count": len(evidence.tradeability),
        "lineage_evidence_count": len(evidence.lineage_evidence),
        "primary_sha256": sha256(evidence.primary_bars),
        "tradeability_sha256": sha256(evidence.tradeability),
        "lineage_evidence_sha256": sha256(evidence.lineage_evidence),
    }
    mismatches = {
        key: {"manifest": manifest.get(key), "actual": actual}
        for key, actual in evidence_checks.items()
        if manifest.get(key) != actual
    }
    if mismatches:
        raise ValueError(f"M2 release evidence does not reconcile: {mismatches}")

    critical_failures = [
        gate.get("name", "unnamed") for gate in manifest.get("gates", [])
        if gate.get("critical") is True and gate.get("passed") is not True
    ]
    if critical_failures:
        raise ValueError(f"M2 release contains failed critical gates: {critical_failures}")

    facts_by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in evidence.tradeability:
        symbol = str(row.get("symbol", ""))
        if len(symbol) != 6 or not symbol.isdigit() or symbol in facts_by_symbol:
            raise ValueError("M2 tradeability scope contains invalid or duplicate symbols")
        if _date(row.get("business_date"), "tradeability business_date") != business_date:
            raise ValueError("M2 tradeability fact is outside the admitted release date")
        facts_by_symbol[symbol] = row
    if len(facts_by_symbol) != policy.expected_symbol_count:
        raise ValueError(
            f"M2 release symbol scope mismatch: expected {policy.expected_symbol_count}, "
            f"got {len(facts_by_symbol)}"
        )

    bars_by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in evidence.primary_bars:
        symbol = str(row.get("symbol", ""))
        if symbol in bars_by_symbol:
            raise ValueError(f"duplicate M2 raw bar for {symbol}")
        if symbol not in facts_by_symbol or _date(row.get("business_date"), "bar business_date") != business_date:
            raise ValueError("M2 raw bar is outside the admitted symbol/date scope")
        if str(row.get("adjustment")) != "none":
            raise ValueError("RQAlpha input must use raw unadjusted M2 bars")
        for field in ("open", "high", "low", "close"):
            _positive_decimal(row.get(field), field)
        bars_by_symbol[symbol] = row

    for symbol, fact in facts_by_symbol.items():
        has_bar = symbol in bars_by_symbol
        if bool(fact.get("has_primary_bar")) != has_bar:
            raise ValueError(f"M2 raw-bar/tradeability mismatch for {symbol}")
        if not has_bar and (
            fact.get("is_suspended") is not True
            or fact.get("can_buy") is not False
            or fact.get("can_sell") is not False
        ):
            raise ValueError(f"missing M2 bar is not fail-closed for {symbol}")

    base_history_dataset_id = str(manifest.get("base_history_dataset_id", "")).strip()
    if not base_history_dataset_id:
        raise ValueError("M2 release requires immutable historical lineage")
    return M2EngineInputRelease(
        release_id=release_id,
        business_date=business_date,
        base_history_dataset_id=base_history_dataset_id,
        manifest_sha256=sha256(manifest),
        primary_bars=tuple(dict(row) for row in evidence.primary_bars),
        tradeability_facts=tuple(dict(row) for row in evidence.tradeability),
        cash_dividends=_cash_dividends(evidence.lineage_evidence, business_date),
    )


def load_engine_input(path: Path, policy: M2AdmissionPolicy) -> M2EngineInputRelease:
    """Load a compact M2 artifact; adjusted factor files stay diagnostic only."""

    from scripts.market_data.tidb_daily_store import load_daily_evidence

    return admit_daily_evidence(load_daily_evidence(path), policy)


class M2ValidatedCorporateActionDataSource:
    """Delegate RQAlpha data while checking target-date actions against M2."""

    _DIVIDEND_FIELDS = frozenset({
        "book_closure_date", "announcement_date", "dividend_cash_before_tax",
        "ex_dividend_date", "payable_date", "round_lot",
    })

    def __init__(self, delegate: Any, release: M2EngineInputRelease) -> None:
        self._delegate = delegate
        self._release = release
        self._dividends = release.dividends_by_order_book_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def get_dividend(self, instrument: Any) -> Any:
        records = self._delegate.get_dividend(instrument)
        order_book_id = str(instrument.order_book_id)
        symbol = order_book_id.split(".", 1)[0]
        if symbol not in self._release.symbols:
            return records
        expected = self._dividends.get(order_book_id)
        if records is None:
            if expected is not None:
                raise RuntimeError(f"RQAlpha dividend is missing for {symbol}")
            return None
        names = frozenset(records.dtype.names or ())
        if not self._DIVIDEND_FIELDS.issubset(names):
            raise RuntimeError("RQAlpha dividend schema is incomplete")
        target_int = int(self._release.business_date.strftime("%Y%m%d"))
        target_rows = records[records["ex_dividend_date"] == target_int]
        if expected is None:
            if len(target_rows):
                raise RuntimeError(f"RQAlpha has an undeclared M2 target-date dividend for {symbol}")
            return records
        if len(target_rows) != 1:
            raise RuntimeError(f"RQAlpha dividend cardinality mismatch for {symbol}")
        row = target_rows[0]
        cash = Decimal(str(float(row["dividend_cash_before_tax"])))
        round_lot = Decimal(str(float(row["round_lot"])))
        if cash != expected.cash_before_tax or round_lot != expected.round_lot:
            raise RuntimeError(f"RQAlpha dividend amount mismatch for {symbol}")
        if int(row["book_closure_date"]) != int(expected.book_closure_date.strftime("%Y%m%d")):
            raise RuntimeError(f"RQAlpha dividend registration date mismatch for {symbol}")
        announcement = int(row["announcement_date"])
        payable = int(row["payable_date"])
        if announcement > target_int or payable < target_int:
            raise RuntimeError(f"RQAlpha dividend lifecycle dates are invalid for {symbol}")
        return records

    def get_split(self, instrument: Any) -> Any:
        records = self._delegate.get_split(instrument)
        symbol = str(instrument.order_book_id).split(".", 1)[0]
        if symbol not in self._release.symbols or records is None:
            return records
        target_int = int(self._release.business_date.strftime("%Y%m%d"))
        ex_dates = records["ex_date"] // 1_000_000
        if len(records[ex_dates == target_int]):
            raise RuntimeError(f"RQAlpha split is not declared by the admitted M2 release for {symbol}")
        return records


__all__ = [
    "CashDividendExpectation",
    "M2AdmissionPolicy",
    "M2EngineInputRelease",
    "M2ValidatedCorporateActionDataSource",
    "PINNED_DAILY_V5_RELEASE_ID",
    "admit_daily_evidence",
    "daily_release_id",
    "load_engine_input",
]

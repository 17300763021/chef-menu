"""Fail-closed gates for point-in-time fundamental evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Iterable, Sequence

from scripts.market_data.fundamental_contracts import (
    SUPPORTED_REPORTING_CURRENCIES,
    FundamentalFact,
    FundamentalReport,
)
from scripts.market_data.quality_gates import GateResult


def evaluate_fundamentals(
    *,
    expected_symbols: Sequence[str],
    reports: Iterable[FundamentalReport],
    facts: Iterable[FundamentalFact],
    successful_symbols: set[str],
    excluded_symbols: set[str] | None = None,
    allowed_excluded_symbols: set[str] | None = None,
) -> list[GateResult]:
    report_rows = list(reports)
    fact_rows = list(facts)
    expected = set(expected_symbols)
    excluded = excluded_symbols or set()
    allowed_excluded = allowed_excluded_symbols or set()
    eligible = expected - excluded
    results: list[GateResult] = []

    invalid_exclusions = sorted(excluded - allowed_excluded)
    results.append(GateResult(
        "fundamental_exclusion_eligibility", not invalid_exclusions, len(invalid_exclusions),
        "= 0 excluded symbols outside the confirmed delisted scope",
        details=tuple(invalid_exclusions[:50]),
    ))

    unexpected = sorted(({row.symbol for row in report_rows} | {row.symbol for row in fact_rows}) - expected)
    results.append(GateResult(
        "fundamental_scope_membership", not unexpected, len(unexpected), "= 0", details=tuple(unexpected[:50]),
    ))

    coverage_bps = len(successful_symbols & eligible) * 10000 // max(1, len(eligible))
    results.append(GateResult(
        "fundamental_symbol_coverage",
        coverage_bps >= 9800,
        f"{coverage_bps / 100:.2f}%",
        ">= 98.00%",
        details=tuple(sorted(eligible - successful_symbols)[:50]),
    ))

    report_keys = [row.key for row in report_rows]
    report_duplicates = sorted(key for key, count in Counter(report_keys).items() if count > 1)
    results.append(GateResult(
        "fundamental_report_duplicate_keys",
        not report_duplicates,
        len(report_duplicates),
        "= 0",
        details=tuple(f"{s}:{t}:{d}:{e}" for s, t, d, e in report_duplicates[:50]),
    ))
    fact_keys = [row.key for row in fact_rows]
    fact_duplicates = sorted(key for key, count in Counter(fact_keys).items() if count > 1)
    results.append(GateResult(
        "fundamental_fact_duplicate_keys", not fact_duplicates, len(fact_duplicates), "= 0",
    ))

    chronology = [
        f"{row.symbol}:{row.statement_type}:{row.report_date}:{row.notice_date}:{row.update_date}"
        for row in report_rows
        if row.notice_date < row.report_date or row.effective_on != max(row.notice_date, row.update_date)
    ]
    results.append(GateResult(
        "fundamental_point_in_time_chronology", not chronology, len(chronology), "= 0", details=tuple(chronology[:50]),
    ))

    report_versions = {row.version_id for row in report_rows}
    orphan_facts = sorted({row.report_version_id for row in fact_rows if row.report_version_id not in report_versions})
    results.append(GateResult(
        "fundamental_fact_report_lineage", not orphan_facts, len(orphan_facts), "= 0", details=tuple(orphan_facts[:20]),
    ))

    currencies = sorted({row.currency for row in report_rows if row.currency not in SUPPORTED_REPORTING_CURRENCIES})
    results.append(GateResult(
        "fundamental_currency_contract", not currencies, len(currencies), "= 0 unsupported or undeclared currencies",
        details=tuple(currencies),
    ))
    report_currency = {row.version_id: row.currency for row in report_rows}
    unit_mismatches = sorted(
        f"{row.symbol}:{row.report_version_id}:{row.unit}:{report_currency.get(row.report_version_id)}"
        for row in fact_rows
        if report_currency.get(row.report_version_id) is not None and row.unit != report_currency[row.report_version_id]
    )
    results.append(GateResult(
        "fundamental_fact_currency_lineage", not unit_mismatches, len(unit_mismatches),
        "= 0 facts whose unit differs from the source report currency", details=tuple(unit_mismatches[:50]),
    ))

    by_report: dict[tuple[str, object, object], dict[str, Decimal]] = defaultdict(dict)
    for fact in fact_rows:
        by_report[(fact.symbol, fact.report_date, fact.effective_on)][fact.metric_code] = fact.value
    equation_issues: list[str] = []
    checked = 0
    for key, metrics in by_report.items():
        if {"TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"}.issubset(metrics):
            checked += 1
            assets = metrics["TOTAL_ASSETS"]
            difference = abs(assets - metrics["TOTAL_LIABILITIES"] - metrics["TOTAL_EQUITY"])
            tolerance = max(Decimal("1"), abs(assets) * Decimal("0.005"))
            if difference > tolerance:
                equation_issues.append(
                    f"{key[0]}:{key[1]}:difference={difference};"
                    f"assets={assets};liabilities={metrics['TOTAL_LIABILITIES']};"
                    f"equity={metrics['TOTAL_EQUITY']}"
                )
    results.append(GateResult(
        "balance_sheet_accounting_equation",
        checked > 0 and not equation_issues,
        f"checked={checked},failed={len(equation_issues)}",
        "checked > 0 and failures = 0 within 0.5%",
        details=tuple(equation_issues[:50]),
    ))

    required_types: dict[str, set[str]] = defaultdict(set)
    for row in report_rows:
        required_types[row.symbol].add(row.statement_type)
    incomplete = sorted(symbol for symbol in successful_symbols if required_types[symbol] != {"balance", "income", "cashflow"})
    results.append(GateResult(
        "fundamental_three_statement_inventory", not incomplete, len(incomplete), "= 0", details=tuple(incomplete[:50]),
    ))
    return results

"""Fail-closed M2.1 source-admission quality gates."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from scripts.market_data.contracts import DailyBar


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    actual: int | str
    threshold: str
    critical: bool = True
    details: tuple[str, ...] = ()

    def canonical(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "actual": self.actual,
            "threshold": self.threshold,
            "critical": self.critical,
            "details": list(self.details),
        }


def evaluate_source(
    primary: Iterable[DailyBar],
    secondary: Iterable[DailyBar],
    symbols: list[str],
    expected_days: int = 60,
) -> list[GateResult]:
    primary_rows = list(primary)
    secondary_rows = list(secondary)
    results: list[GateResult] = []

    for name, rows in (("primary_nonempty", primary_rows), ("secondary_nonempty", secondary_rows)):
        results.append(GateResult(name, bool(rows), len(rows), "> 0"))

    keys = [row.key for row in primary_rows]
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    results.append(GateResult("primary_duplicate_keys", not duplicates, len(duplicates), "= 0", details=tuple(f"{s}:{d}" for s, d in duplicates[:20])))

    invalid_ohlc = []
    invalid_amounts = []
    for row in primary_rows:
        if row.low > min(row.open, row.close) or row.high < max(row.open, row.close) or row.low > row.high:
            invalid_ohlc.append(f"{row.symbol}:{row.business_date.isoformat()}")
        if row.volume_shares < 0 or row.amount_cny < 0:
            invalid_amounts.append(f"{row.symbol}:{row.business_date.isoformat()}")
    results.append(GateResult("primary_ohlc_invariants", not invalid_ohlc, len(invalid_ohlc), "= 0", details=tuple(invalid_ohlc[:20])))
    results.append(GateResult("primary_nonnegative_units", not invalid_amounts, len(invalid_amounts), "= 0", details=tuple(invalid_amounts[:20])))

    by_symbol: dict[str, set] = defaultdict(set)
    for row in primary_rows:
        by_symbol[row.symbol].add(row.business_date)
    expected = max(1, len(symbols) * expected_days)
    observed = sum(min(expected_days, len(by_symbol.get(symbol, set()))) for symbol in symbols)
    coverage_bps = (observed * 10000) // expected
    missing_symbols = tuple(symbol for symbol in symbols if not by_symbol.get(symbol))
    results.append(GateResult("primary_coverage", coverage_bps >= 9800, f"{coverage_bps / 100:.2f}%", ">= 98.00%", details=missing_symbols))

    primary_map = {row.key: row for row in primary_rows}
    secondary_map = {row.key: row for row in secondary_rows}
    common = sorted(primary_map.keys() & secondary_map.keys())
    match_coverage_bps = (len(common) * 10000) // max(1, len(primary_map))
    results.append(GateResult("cross_source_pair_coverage", match_coverage_bps >= 9500, f"{match_coverage_bps / 100:.2f}%", ">= 95.00%"))

    mismatches = []
    for key in common:
        first = primary_map[key]
        second = secondary_map[key]
        tolerance = max(Decimal("0.01"), abs(first.close) * Decimal("0.0005"))
        if abs(first.close - second.close) > tolerance:
            mismatches.append(f"{key[0]}:{key[1].isoformat()}:{first.close}:{second.close}")
    consistency_bps = ((len(common) - len(mismatches)) * 10000) // max(1, len(common))
    results.append(GateResult("cross_source_close_consistency", bool(common) and consistency_bps >= 9950, f"{consistency_bps / 100:.2f}%", ">= 99.50%", details=tuple(mismatches[:20])))

    volume_mismatches = []
    amount_mismatches = []
    for key in common:
        first = primary_map[key]
        second = secondary_map[key]
        volume_tolerance = max(100, int(second.volume_shares * 0.001))
        if abs(first.volume_shares - second.volume_shares) > volume_tolerance:
            volume_mismatches.append(f"{key[0]}:{key[1].isoformat()}:{first.volume_shares}:{second.volume_shares}")
        amount_tolerance = max(Decimal("1.00"), abs(second.amount_cny) * Decimal("0.0001"))
        if abs(first.amount_cny - second.amount_cny) > amount_tolerance:
            amount_mismatches.append(f"{key[0]}:{key[1].isoformat()}:{first.amount_cny}:{second.amount_cny}")
    volume_bps = ((len(common) - len(volume_mismatches)) * 10000) // max(1, len(common))
    amount_bps = ((len(common) - len(amount_mismatches)) * 10000) // max(1, len(common))
    results.append(GateResult("cross_source_volume_unit_consistency", bool(common) and volume_bps >= 9950, f"{volume_bps / 100:.2f}%", ">= 99.50%", details=tuple(volume_mismatches[:20])))
    results.append(GateResult("cross_source_amount_consistency", bool(common) and amount_bps >= 9950, f"{amount_bps / 100:.2f}%", ">= 99.50%", details=tuple(amount_mismatches[:20])))
    return results


def accepted(results: Iterable[GateResult]) -> bool:
    return all(result.passed for result in results if result.critical)

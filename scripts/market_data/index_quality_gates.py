"""Quality gates for CSI benchmark index histories."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from datetime import date
from typing import Iterable

from scripts.market_data.index_bars import INDEX_CODES, IndexBar
from scripts.market_data.quality_gates import GateResult


def evaluate_index_bars(
    primary: Iterable[IndexBar], verification: Iterable[IndexBar], expected_sessions: set[date] | None = None,
) -> list[GateResult]:
    first = list(primary)
    second = list(verification)
    results: list[GateResult] = []
    results.append(GateResult("index_inventory", {row.index_code for row in first} == set(INDEX_CODES), sorted({row.index_code for row in first}), "= 000300,000905"))
    if expected_sessions is not None:
        date_issues: list[str] = []
        for code in INDEX_CODES:
            observed = {row.business_date for row in first if row.index_code == code}
            date_issues.extend(f"{code}:missing:{value}" for value in sorted(expected_sessions - observed)[:50])
            date_issues.extend(f"{code}:extra:{value}" for value in sorted(observed - expected_sessions)[:50])
        results.append(GateResult(
            "index_trading_calendar_alignment", not date_issues, len(date_issues), "= 0", details=tuple(date_issues[:50]),
        ))
    keys = [row.key for row in first]
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    results.append(GateResult("index_duplicate_keys", not duplicates, len(duplicates), "= 0"))
    invalid = [f"{row.index_code}:{row.business_date}" for row in first if row.low > min(row.open, row.close) or row.high < max(row.open, row.close) or row.low > row.high]
    results.append(GateResult("index_ohlc_invariants", not invalid, len(invalid), "= 0", details=tuple(invalid[:50])))
    official_count = sum(row.source == "csi_official_history" for row in first)
    official_coverage_bps = official_count * 10000 // max(1, len(first))
    results.append(GateResult(
        "index_official_primary_coverage", official_coverage_bps >= 9990,
        f"{official_coverage_bps / 100:.2f}%", ">= 99.90%",
        details=tuple(f"{row.index_code}:{row.business_date}" for row in first if row.source != "csi_official_history"),
    ))
    pmap = {row.key: row for row in first}
    smap = {row.key: row for row in second}
    missing_pairs = sorted(set(pmap) - set(smap))
    results.append(GateResult("index_cross_source_date_alignment", not missing_pairs and bool(pmap), len(missing_pairs), "= 0", details=tuple(f"{a}:{b}" for a, b in missing_pairs[:50])))
    price_mismatches: list[str] = []
    volume_mismatches: list[str] = []
    independently_compared = 0
    for key in sorted(set(pmap) & set(smap)):
        left, right = pmap[key], smap[key]
        if left.source == "csi_official_history":
            independently_compared += 1
        if abs(left.close - right.close) > max(Decimal("0.02"), abs(left.close) * Decimal("0.0001")):
            price_mismatches.append(f"{key[0]}:{key[1]}:{left.close}:{right.close}")
        if left.volume_shares is not None and right.volume_shares is not None:
            tolerance = max(1000, int(left.volume_shares * Decimal("0.001")))
            if abs(left.volume_shares - right.volume_shares) > tolerance:
                volume_mismatches.append(f"{key[0]}:{key[1]}:{left.volume_shares}:{right.volume_shares}")
    results.append(GateResult("index_cross_source_close", not price_mismatches, len(price_mismatches), "= 0", details=tuple(price_mismatches[:50])))
    results.append(GateResult("index_cross_source_volume", not volume_mismatches, len(volume_mismatches), "= 0", details=tuple(volume_mismatches[:50])))
    independent_bps = independently_compared * 10000 // max(1, len(pmap))
    results.append(GateResult(
        "index_independent_pair_coverage", independent_bps >= 9990,
        f"{independent_bps / 100:.2f}%", ">= 99.90%",
    ))
    return results

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from scripts.market_data.contracts import DailyBar
from scripts.market_data.manifest import build_manifest
from scripts.market_data.quality_gates import accepted, evaluate_source
from scripts.market_data.sample_capture import write_outputs


def bar(source: str, symbol: str, day: date, close: str = "10.00") -> DailyBar:
    price = Decimal(close)
    return DailyBar(
        source=source,
        symbol=symbol,
        exchange="SSE" if symbol.startswith("6") else "SZSE",
        business_date=day,
        open=price,
        high=price + Decimal("0.10"),
        low=price - Decimal("0.10"),
        close=price,
        previous_close=price,
        volume_shares=10000,
        amount_cny=price * 10000,
        turnover_percent=Decimal("0.10"),
        trade_status="trading",
        is_st=False,
    )


class MarketDataQualityTests(unittest.TestCase):
    def healthy_rows(self) -> tuple[list[DailyBar], list[DailyBar]]:
        start = date(2026, 7, 14)
        primary = [bar("akshare_eastmoney", symbol, start + timedelta(days=offset)) for symbol in ("600519", "000001") for offset in range(2)]
        secondary = [replace(item, source="baostock") for item in primary]
        return primary, secondary

    def test_healthy_fixture_passes_all_critical_gates(self) -> None:
        primary, secondary = self.healthy_rows()
        gates = evaluate_source(primary, secondary, ["600519", "000001"], expected_days=2)
        self.assertTrue(accepted(gates), [gate.canonical() for gate in gates])

    def test_duplicate_bad_ohlc_and_price_conflict_fail(self) -> None:
        primary, secondary = self.healthy_rows()
        primary.append(primary[0])
        primary[1] = replace(primary[1], high=Decimal("9.00"))
        secondary[2] = replace(secondary[2], close=Decimal("11.00"))
        gates = {gate.name: gate for gate in evaluate_source(primary, secondary, ["600519", "000001"], expected_days=2)}
        self.assertFalse(gates["primary_duplicate_keys"].passed)
        self.assertFalse(gates["primary_ohlc_invariants"].passed)
        self.assertFalse(gates["cross_source_close_consistency"].passed)

    def test_share_lot_unit_regression_fails(self) -> None:
        primary, secondary = self.healthy_rows()
        primary = [replace(item, volume_shares=item.volume_shares * 100) for item in primary]
        gates = {gate.name: gate for gate in evaluate_source(primary, secondary, ["600519", "000001"], expected_days=2)}
        self.assertFalse(gates["cross_source_volume_unit_consistency"].passed)

    def test_empty_secondary_and_low_coverage_fail_closed(self) -> None:
        primary, _ = self.healthy_rows()
        gates = evaluate_source(primary[:1], [], ["600519", "000001"], expected_days=2)
        self.assertFalse(accepted(gates))

    def test_manifest_and_compressed_payload_are_deterministic(self) -> None:
        primary, secondary = self.healthy_rows()
        gates = evaluate_source(primary, secondary, ["600519", "000001"], expected_days=2)
        sample = {"symbols": ["600519", "000001"], "requested_end": "2026-07-15"}
        first = build_manifest([*primary, *secondary], gates, sample)
        second = build_manifest([*reversed(secondary), *reversed(primary)], gates, sample)
        self.assertEqual(first["dataset_sha256"], second["dataset_sha256"])
        with tempfile.TemporaryDirectory() as directory:
            left = Path(directory) / "left"
            right = Path(directory) / "right"
            write_outputs(left, first, [*primary, *secondary])
            write_outputs(right, second, [*reversed(secondary), *reversed(primary)])
            self.assertEqual((left / "normalized-bars.json.gz").read_bytes(), (right / "normalized-bars.json.gz").read_bytes())


if __name__ == "__main__":
    unittest.main()

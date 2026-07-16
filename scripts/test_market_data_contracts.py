from __future__ import annotations

import unittest

from scripts.market_data.contracts import (
    baostock_symbol,
    exchange_for_symbol,
    normalize_akshare_row,
    normalize_baostock_row,
    normalize_symbol,
)


class MarketDataContractTests(unittest.TestCase):
    def test_symbol_and_exchange_normalization(self) -> None:
        self.assertEqual(normalize_symbol("sh.600519"), "600519")
        self.assertEqual(exchange_for_symbol("600519"), "SSE")
        self.assertEqual(exchange_for_symbol("300750"), "SZSE")
        self.assertEqual(baostock_symbol("000001"), "sz.000001")
        with self.assertRaises(ValueError):
            normalize_symbol("AAPL")

    def test_akshare_contract_converts_lots_to_shares(self) -> None:
        bar = normalize_akshare_row(
            {
                "日期": "2026-07-15",
                "股票代码": "600519",
                "开盘": "1410.00",
                "最高": "1425.00",
                "最低": "1400.00",
                "收盘": "1420.00",
                "成交量": "1234",
                "成交额": "175228000",
                "换手率": "0.10",
            },
            "600519",
        )
        self.assertEqual(bar.volume_shares, 123_400)
        self.assertEqual(bar.canonical()["close"], "1420.0000")
        self.assertEqual(bar.adjustment, "none")

    def test_baostock_contract_keeps_share_unit_and_status(self) -> None:
        bar = normalize_baostock_row(
            {
                "date": "2026-07-15",
                "code": "sz.000001",
                "open": "11.10",
                "high": "11.30",
                "low": "11.00",
                "close": "11.20",
                "preclose": "11.05",
                "volume": "10000",
                "amount": "112000",
                "turn": "0.01",
                "tradestatus": "1",
                "isST": "0",
            },
            "000001",
        )
        self.assertEqual(bar.volume_shares, 10_000)
        self.assertEqual(bar.trade_status, "trading")
        self.assertFalse(bar.is_st)

    def test_missing_price_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing close"):
            normalize_baostock_row(
                {
                    "date": "2026-07-15", "code": "sh.600519", "open": "1", "high": "1",
                    "low": "1", "close": "", "preclose": "1", "volume": "1", "amount": "1",
                    "turn": "1", "tradestatus": "1", "isST": "0",
                },
                "600519",
            )


if __name__ == "__main__":
    unittest.main()

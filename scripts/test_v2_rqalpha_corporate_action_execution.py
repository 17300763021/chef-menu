"""Cloud-only proof that RQAlpha, not custom accounting, applies dividends."""

from __future__ import annotations

import unittest
from collections import deque
from datetime import date
from decimal import Decimal
from types import MethodType, SimpleNamespace

import numpy as np
import pandas as pd
from rqalpha.mod.rqalpha_mod_sys_accounts.position_model import StockPosition

from scripts.simulation.m2_data_source import (
    CashDividendExpectation,
    M2EngineInputRelease,
    M2ValidatedCorporateActionDataSource,
)


DIVIDEND_DTYPE = [
    ("book_closure_date", "<i8"),
    ("announcement_date", "<i8"),
    ("dividend_cash_before_tax", "<f8"),
    ("ex_dividend_date", "<i8"),
    ("payable_date", "<i8"),
    ("round_lot", "<f8"),
]


def _release(symbol: str, cash_per_ten: str) -> M2EngineInputRelease:
    target = date(2026, 7, 31)
    expectation = CashDividendExpectation(
        symbol=symbol,
        book_closure_date=date(2026, 7, 30),
        ex_dividend_date=target,
        cash_before_tax=Decimal(cash_per_ten),
        round_lot=Decimal("10"),
        evidence_source="m2-regression-fixture",
        evidence_sha256="a" * 64,
    )
    return M2EngineInputRelease(
        release_id="m2-diagnostic-regression",
        business_date=target,
        base_history_dataset_id="m2-full-fixed-history",
        manifest_sha256="b" * 64,
        primary_bars=(),
        tradeability_facts=({"symbol": symbol},),
        cash_dividends=(expectation,),
    )


def _records(cash_per_ten: str) -> np.ndarray:
    return np.array(
        [(20260730, 20260724, float(cash_per_ten), 20260731, 20260731, 10.0)],
        dtype=DIVIDEND_DTYPE,
    )


def _execute(symbol: str, close: str, cash_per_ten: str) -> tuple[float, object]:
    target = date(2026, 7, 31)
    release = _release(symbol, cash_per_ten)
    expectation = release.cash_dividends[0]
    records = _records(cash_per_ten)

    class Delegate:
        def get_dividend(self, _instrument):
            return records

    overlay = M2ValidatedCorporateActionDataSource(Delegate(), release)
    dividends = overlay.get_dividend(SimpleNamespace(order_book_id=expectation.order_book_id))

    harness = SimpleNamespace(
        _all_dividends=dividends,
        _avg_price=float(close),
        _last_price=float(close),
        _quantity=1000,
        _dividend_receivable=deque(),
        _historical_dividends=pd.Series(dtype=float),
        _env=SimpleNamespace(
            data_proxy=SimpleNamespace(get_previous_trading_date=lambda _date: date(2026, 7, 30))
        ),
    )
    harness._get_dividends_or_splits = MethodType(StockPosition._get_dividends_or_splits, harness)
    receivable = StockPosition._handle_dividend_book_closure(harness, target, harness._env.data_proxy)
    return receivable, harness


class RQAlphaCorporateActionExecutionTests(unittest.TestCase):
    def test_missing_or_conflicting_rqalpha_action_fails_closed(self) -> None:
        instrument = SimpleNamespace(order_book_id="601727.XSHG")

        class MissingDelegate:
            def get_dividend(self, _instrument):
                return None

        with self.assertRaisesRegex(RuntimeError, "missing"):
            M2ValidatedCorporateActionDataSource(
                MissingDelegate(), _release("601727", "0.1425")
            ).get_dividend(instrument)

        class ConflictingDelegate:
            def get_dividend(self, _instrument):
                return _records("0.1500")

        with self.assertRaisesRegex(RuntimeError, "amount mismatch"):
            M2ValidatedCorporateActionDataSource(
                ConflictingDelegate(), _release("601727", "0.1425")
            ).get_dividend(instrument)

    def test_601727_exact_cash_is_applied_by_rqalpha(self) -> None:
        receivable, position = _execute("601727", "6.8900", "0.1425")
        self.assertAlmostEqual(receivable, 14.25)
        self.assertAlmostEqual(position._avg_price, 6.87575)
        payable_date, payable_amount = position._dividend_receivable[0]
        self.assertEqual(payable_date, date(2026, 7, 31))
        self.assertAlmostEqual(payable_amount, 14.25)

    def test_601866_half_tick_cash_is_applied_by_rqalpha(self) -> None:
        receivable, position = _execute("601866", "2.4700", "0.1500")
        self.assertAlmostEqual(receivable, 15.0)
        self.assertAlmostEqual(position._avg_price, 2.455)


if __name__ == "__main__":
    unittest.main()

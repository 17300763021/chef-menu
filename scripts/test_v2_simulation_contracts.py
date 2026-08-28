from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.simulation.contracts import DataState, MarketState, OrderInstruction, PositionState
from scripts.simulation.rqalpha_adapter import RQAlphaAdapter, rqalpha_order_book_id


def instruction(**changes):
    row = {
        "instruction_id": "instruction-1", "symbol": "000001", "side": "buy", "quantity": 100,
        "price_type": "limit", "limit_price": "10.0000", "business_date": "2026-07-31",
        "valid_until": "2026-07-31", "strategy_version": "strategy-v1", "data_release_id": "m2-release-1",
    }
    row.update(changes)
    return OrderInstruction.from_mapping(row)


def market(**changes):
    row = {
        "symbol": "000001", "business_date": date(2026, 7, 31), "data_state": DataState.FRESH,
        "data_release_id": "m2-release-1", "last_price": Decimal("10"),
        "previous_close": Decimal("9.8"), "upper_limit": Decimal("10.78"),
        "lower_limit": Decimal("8.82"), "suspended": False,
        "one_price_limit_up": False, "one_price_limit_down": False,
    }
    row.update(changes)
    return MarketState(**row)


class StructuredOrderContractTest(unittest.TestCase):
    def test_text_price_range_cannot_become_an_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "numeric structured field"):
            instruction(limit_price="建议在 9.80-10.00 买入")

    def test_missing_limit_price_is_non_actionable(self) -> None:
        with self.assertRaisesRegex(ValueError, "require a structured limit_price"):
            instruction(limit_price=None)

    def test_market_order_rejects_hidden_limit_price(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot carry"):
            instruction(price_type="market", limit_price="10")

    def test_a_share_exchange_mapping_is_explicit(self) -> None:
        self.assertEqual(rqalpha_order_book_id("000001"), "000001.XSHE")
        self.assertEqual(rqalpha_order_book_id("600000"), "600000.XSHG")
        self.assertEqual(rqalpha_order_book_id("830799"), "830799.BJSE")


class RQAlphaPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = RQAlphaAdapter()

    def assertRejected(self, expected: str, order=None, state=None, position=None) -> None:
        result = self.adapter.preflight(order or instruction(), state or market(), position)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, expected)

    def test_valid_buy_is_signed_for_public_order_shares_api(self) -> None:
        result = self.adapter.preflight(instruction(), market(), None)
        self.assertTrue(result.accepted)
        self.assertEqual(result.signed_quantity, 100)
        self.assertEqual(result.rqalpha_order_book_id, "000001.XSHE")

    def test_buy_board_lot_is_fail_closed(self) -> None:
        self.assertRejected("board_lot", instruction(quantity=101))

    def test_stale_suspended_and_limit_up_are_fail_closed(self) -> None:
        self.assertRejected("market_data_stale", state=market(data_state=DataState.STALE))
        self.assertRejected("suspended", state=market(suspended=True))
        self.assertRejected("one_price_limit_up", state=market(one_price_limit_up=True))

    def test_missing_tradeability_flag_is_not_assumed_false(self) -> None:
        self.assertRejected("limit_state_missing", state=market(one_price_limit_up=None))

    def test_limit_price_must_be_inside_daily_band(self) -> None:
        self.assertRejected("limit_price_out_of_range", instruction(limit_price="10.79"))

    def test_t_plus_one_and_position_availability_are_fail_closed(self) -> None:
        order = instruction(side="sell", quantity=100)
        position = PositionState("000001", 200, 0, Decimal("9.5"))
        self.assertRejected("t_plus_one", order, market(), position)
        self.assertRejected("insufficient_position", instruction(side="sell", quantity=300), market(), position)

    def test_one_price_limit_down_blocks_sell(self) -> None:
        order = instruction(side="sell")
        position = PositionState("000001", 100, 100, Decimal("9.5"))
        self.assertRejected("one_price_limit_down", order, market(one_price_limit_down=True), position)

    def test_submit_calls_only_pinned_public_api_shape(self) -> None:
        calls = []
        with patch.object(self.adapter, "assert_framework_version", return_value="6.2.1"):
            result = self.adapter.submit(
                instruction(), market(), None,
                lambda order_book_id, amount, **kwargs: calls.append((order_book_id, amount, kwargs)) or "order",
            )
        self.assertEqual(result, "order")
        self.assertEqual(calls, [("000001.XSHE", 100, {"price": 10.0})])

    def test_strategy_runs_only_through_public_run_func_shape(self) -> None:
        calls = []
        init = lambda context: None
        handle_bar = lambda context, bars: None
        with patch.object(self.adapter, "assert_framework_version", return_value="6.2.1"):
            result = self.adapter.run_strategy(
                config={"base": {"start_date": "2026-07-31", "run_type": "b", "accounts": {"stock": 100000}}},
                init=init, handle_bar=handle_bar,
                run_function=lambda **kwargs: calls.append(kwargs) or {"sys_analyser": {}},
            )
        self.assertEqual(result, {"sys_analyser": {}})
        self.assertEqual(set(calls[0]), {"config", "init", "handle_bar"})

    def test_non_backtest_or_missing_virtual_account_config_is_rejected(self) -> None:
        callback = lambda *args: None
        with self.assertRaisesRegex(ValueError, "backtest mode only"):
            self.adapter.run_strategy(
                config={"base": {"run_type": "p", "accounts": {"stock": 100000}}},
                init=callback, handle_bar=callback, run_function=lambda **kwargs: {},
            )
        with self.assertRaisesRegex(ValueError, "simulation-only stock account"):
            self.adapter.run_strategy(
                config={"base": {"run_type": "b", "accounts": {}}},
                init=callback, handle_bar=callback, run_function=lambda **kwargs: {},
            )


if __name__ == "__main__":
    unittest.main()

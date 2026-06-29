from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from paper_trade_engine import buy_position, record_skipped_sell_decision, sell_decision, sell_position


class FakeSupabaseClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object | None, str | None]] = []

    def request(self, method: str, path: str, body=None, prefer: str | None = None):
        self.requests.append((method, path, body, prefer))
        if method == "GET" and path.startswith("stock_signal_events?"):
            return [{"id": "signal-1"}]
        if method == "POST" and path == "stock_auto_trade_orders":
            return [{"id": "order-1"}]
        return []

    def insert(self, table: str, rows: list[dict]) -> int:
        self.requests.append(("INSERT", table, rows, None))
        return len(rows)


class PaperTradeExecutionStatusTest(unittest.TestCase):
    def test_invalid_buy_price_marks_signal_failed_instead_of_silent_skip(self) -> None:
        client = FakeSupabaseClient()
        decision = {
            "code": "000001",
            "name": "平安银行",
            "decision_date": "2026-06-25",
            "current_price": 0,
            "suggest_buy_price": 0,
            "can_buy": True,
            "final_action": "可以买小仓",
        }

        result = buy_position(client, decision, [], [])

        self.assertIsNone(result)
        patches = [
            request for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_signal_events?")
        ]
        self.assertEqual(len(patches), 1)
        payload = patches[0][2]
        self.assertEqual(payload["execution_status"], "failed")
        self.assertIn("价格无效", payload["execution_reason"])

    def test_stop_loss_sell_decision_clears_all_shares(self) -> None:
        result = sell_decision(
            {"current_price": 9.8, "stop_loss": 10, "target_price_1": 11},
            {"shares": 1000, "cost_price": 10, "sell_stage": "none"},
        )

        self.assertEqual(result.reason, "触发止损")
        self.assertEqual(result.shares, 1000)
        self.assertEqual(result.next_sell_stage, "closed")

    def test_first_r_sell_decision_sells_half_lot(self) -> None:
        result = sell_decision(
            {"current_price": 11, "stop_loss": 9, "target_price_1": 11},
            {"shares": 1000, "cost_price": 10, "sell_stage": "none"},
        )

        self.assertEqual(result.reason, "触发第一止盈位")
        self.assertEqual(result.shares, 500)
        self.assertEqual(result.next_sell_stage, "sold_1r")
        self.assertEqual(result.last_profit_taking_price, 11)

    def test_second_r_sell_decision_sells_remaining_normal_stock(self) -> None:
        result = sell_decision(
            {"current_price": 12, "stop_loss": 9, "target_price_1": 11, "change_rate": 4},
            {"shares": 500, "cost_price": 10, "sell_stage": "sold_1r"},
        )

        self.assertEqual(result.reason, "触发第二止盈位")
        self.assertEqual(result.shares, 500)
        self.assertEqual(result.next_sell_stage, "closed")

    def test_strong_limit_up_updates_trailing_stop_without_selling(self) -> None:
        result = sell_decision(
            {"current_price": 12, "stop_loss": 9, "target_price_1": 11, "change_rate": 10.01},
            {"shares": 500, "cost_price": 10, "sell_stage": "sold_1r", "trailing_stop_price": 10.5},
        )

        self.assertEqual(result.reason, "强势涨停，暂不机械止盈，抬高移动止损")
        self.assertEqual(result.shares, 0)
        self.assertEqual(result.execution_status, "blocked")
        self.assertEqual(result.next_sell_stage, "trailing_stop")
        self.assertGreater(result.trailing_stop_price, 10.5)

    def test_trailing_stop_break_clears_remaining_shares(self) -> None:
        result = sell_decision(
            {"current_price": 10.9, "stop_loss": 9, "target_price_1": 11, "change_rate": -3},
            {"shares": 500, "cost_price": 10, "sell_stage": "trailing_stop", "trailing_stop_price": 11},
        )

        self.assertEqual(result.reason, "跌破移动止损")
        self.assertEqual(result.shares, 500)
        self.assertEqual(result.next_sell_stage, "closed")

    def test_partial_sell_writes_next_sell_stage_to_position(self) -> None:
        client = FakeSupabaseClient()
        decision = {"code": "000001", "name": "平安银行", "current_price": 11, "target_price_1": 11}
        position = {
            "id": "position-1",
            "code": "000001",
            "name": "平安银行",
            "shares": 1000,
            "cost_price": 10,
            "buy_date": "2026-06-25",
            "sell_stage": "none",
        }
        decision_result = sell_decision(decision, position)

        sell_position(client, decision, position, [position], [], decision_result.reason, decision_result.shares, decision_result)

        position_patches = [
            request for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_positions?")
        ]
        self.assertTrue(position_patches)
        payload = position_patches[-1][2]
        self.assertEqual(payload["shares"], 500)
        self.assertEqual(payload["sell_stage"], "sold_1r")
        self.assertEqual(payload["last_profit_taking_price"], 11)

    def test_strong_limit_up_skip_records_blocked_signal_and_trailing_stop(self) -> None:
        client = FakeSupabaseClient()
        decision = {
            "code": "000001",
            "name": "平安银行",
            "current_price": 12,
            "target_price_1": 11,
            "change_rate": 10.01,
        }
        position = {
            "id": "position-1",
            "code": "000001",
            "name": "平安银行",
            "shares": 500,
            "cost_price": 10,
            "sell_stage": "sold_1r",
            "trailing_stop_price": 10.5,
        }
        decision_result = sell_decision(decision, position)

        record_skipped_sell_decision(client, decision, position, decision_result)

        position_patch = [
            request for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_positions?")
        ][0][2]
        signal_patch = [
            request for request in client.requests
            if request[0] == "PATCH" and request[1].startswith("stock_signal_events?")
        ][0][2]
        self.assertEqual(position_patch["sell_stage"], "trailing_stop")
        self.assertGreater(position_patch["trailing_stop_price"], 10.5)
        self.assertEqual(signal_patch["execution_status"], "blocked")
        self.assertIn("强势涨停", signal_patch["execution_reason"])


if __name__ == "__main__":
    unittest.main()

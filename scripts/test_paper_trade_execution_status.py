from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from paper_trade_engine import buy_position


class FakeSupabaseClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object | None, str | None]] = []

    def request(self, method: str, path: str, body=None, prefer: str | None = None):
        self.requests.append((method, path, body, prefer))
        if method == "GET" and path.startswith("stock_signal_events?"):
            return [{"id": "signal-1"}]
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


if __name__ == "__main__":
    unittest.main()

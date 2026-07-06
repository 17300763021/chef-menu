import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_trade_engine as engine


def prediction(rank=1, confidence=0.7, predicted_return=2.0, price=10.0):
    return {
        "id": "prediction-1",
        "prediction_date": "2026-07-06",
        "code": "000001",
        "name": "Ping An",
        "rank": rank,
        "confidence": confidence,
        "predicted_return": predicted_return,
        "close_price": price,
        "feature_payload": {"return_5d": 2},
        "model_name": engine.MODEL_NAME,
        "model_version": engine.MODEL_VERSION,
    }


class ModelTradeEngineTest(unittest.TestCase):
    def test_low_confidence_blocks_trade(self):
        decision = engine.make_decision(prediction(confidence=0.1), None)

        self.assertEqual(decision["action"], "blocked")
        self.assertEqual(decision["risk_gate_status"], "blocked")

    def test_top_rank_positive_prediction_buys(self):
        decision = engine.make_decision(prediction(rank=2, predicted_return=1.5), None)

        self.assertEqual(decision["action"], "buy")
        self.assertGreater(decision["target_weight"], 0)

    def test_existing_position_sells_when_rank_deteriorates(self):
        position = {"cost_price": 10, "shares": 1000, "buy_date": "2026-07-01"}
        decision = engine.make_decision(prediction(rank=80, predicted_return=-1), position)

        self.assertEqual(decision["action"], "sell")
        self.assertEqual(decision["planned_shares"], 1000)

    def test_existing_position_holds_when_model_remains_constructive(self):
        position = {"cost_price": 10, "shares": 1000, "buy_date": "2026-07-01"}
        decision = engine.make_decision(prediction(rank=5, predicted_return=2), position)

        self.assertEqual(decision["action"], "hold")

    def test_round_lot_blocks_less_than_one_lot(self):
        self.assertEqual(engine.round_lot(99), 0)
        self.assertEqual(engine.round_lot(199), 100)

    def test_t_plus_one_sell_is_blocked(self):
        calls = []

        class Client:
            def request(self, method, path, body=None, prefer=None):
                calls.append((method, path, body, prefer))
                if method == "POST" and path == "stock_model_orders":
                    return [{"id": "order-blocked"}]
                return []

        engine.sell_position(
            Client(),
            prediction(),
            {"id": "pos-1", "code": "000001", "name": "Ping An", "cost_price": 10, "shares": 100, "buy_date": "2026-07-06"},
            [],
            [],
            "decision-1",
            "rank deteriorated",
        )

        order_call = calls[0]
        self.assertEqual(order_call[2]["status"], "blocked")
        self.assertEqual(order_call[2]["failure_reason"], "T+1 same-day sell blocked")


if __name__ == "__main__":
    unittest.main()

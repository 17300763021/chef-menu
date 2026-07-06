import unittest
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_prediction_engine as engine


def history(code="000001", start=1, count=70, base=10.0):
    rows = []
    for index in range(count):
        trade_date = date(2026, 4, 1) + timedelta(days=index)
        price = base + index * 0.05
        rows.append({
            "code": code,
            "trade_date": trade_date.isoformat(),
            "open": price - 0.02,
            "high": price + 0.08,
            "low": price - 0.08,
            "close": price,
            "volume": 100000 + index * 1000,
        })
    return rows


class ModelPredictionEngineTest(unittest.TestCase):
    def test_prediction_does_not_use_future_dates(self):
        rows = history(count=40)
        prediction = engine.build_prediction("000001", rows)

        self.assertIsNotNone(prediction)
        assert prediction is not None
        self.assertEqual(prediction["prediction_date"], rows[-1]["trade_date"])
        self.assertEqual(prediction["feature_window_end"], rows[-1]["trade_date"])
        self.assertLessEqual(prediction["train_end_date"], prediction["prediction_date"])
        self.assertLessEqual(prediction["validation_end_date"], prediction["prediction_date"])
        self.assertLessEqual(prediction["test_end_date"], prediction["prediction_date"])

    def test_predictions_are_reproducible_for_same_input(self):
        rows = history("000001", count=70) + history("000002", count=70, base=8.0)

        first = engine.build_predictions(rows)
        second = engine.build_predictions(rows)

        self.assertEqual(first, second)
        self.assertEqual([item["rank"] for item in first], [1, 2])

    def test_prediction_payload_contains_required_model_fields(self):
        rows = history(count=70)
        prediction = engine.build_prediction("000001", rows)

        self.assertIsNotNone(prediction)
        assert prediction is not None
        self.assertEqual(prediction["model_name"], engine.MODEL_NAME)
        self.assertEqual(prediction["model_version"], engine.MODEL_VERSION)
        self.assertEqual(prediction["feature_set"], engine.FEATURE_SET)
        self.assertIn("return_20d", prediction["feature_payload"])
        self.assertGreater(prediction["confidence"], 0)


if __name__ == "__main__":
    unittest.main()

import unittest
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_prediction_engine as engine
import model_trainer


class FakeTrainingClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if "offset=0" in path:
            return [
                {"code": "000001", "trade_date": "2026-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
            ]
        if "offset=1" in path:
            return [
                {"code": "000002", "trade_date": "2026-01-01", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2}
            ]
        return []


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
    def test_load_training_data_paginates_supabase_rows(self):
        client = FakeTrainingClient()

        rows = model_trainer.load_training_data(client, lookback_days=0, page_size=1)

        self.assertEqual([row["code"] for row in rows], ["000001", "000002"])
        self.assertEqual(len(client.calls), 3)

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

    def test_predictions_exclude_stale_history(self):
        current_rows = history("000001", count=70)
        stale_rows = history("000002", count=60, base=20.0)

        predictions = engine.build_predictions(current_rows + stale_rows)

        self.assertEqual([item["code"] for item in predictions], ["000001"])

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

    def test_real_model_reproducibility(self):
        rows = history(count=90)
        with tempfile.TemporaryDirectory() as tmp:
            model_trainer.save_model_bundle(
                model_trainer.train_stacking_model(model_trainer.synthetic_training_frame()),
                Path(tmp),
                version="20990101",
            )

            first = engine.build_prediction("000001", rows, model_store_dir=tmp)
            second = engine.build_prediction("000001", rows, model_store_dir=tmp)

        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_no_future_data_in_features(self):
        rows = history(count=90)
        truncated = rows[:-5]
        with_future = rows[:]

        features_before = model_trainer.extract_features_for_code("000001", truncated)
        features_with_future_trimmed = model_trainer.extract_features_for_code(
            "000001",
            [row for row in with_future if row["trade_date"] <= truncated[-1]["trade_date"]],
        )

        pd.testing.assert_frame_equal(features_before, features_with_future_trimmed)

    def test_model_fallback_when_no_model_file(self):
        rows = history(count=90)
        with tempfile.TemporaryDirectory() as tmp:
            prediction = engine.build_prediction("000001", rows, model_store_dir=tmp)

        self.assertIsNotNone(prediction)
        assert prediction is not None
        self.assertEqual(prediction["feature_payload"].get("prediction_source"), "fallback_linear")

    def test_stacking_vs_single_model(self):
        result = model_trainer.train_stacking_model(model_trainer.synthetic_training_frame())

        self.assertGreater(result.metrics["stacking"]["rank_ic"], 0.03)
        self.assertGreater(result.metrics["stacking"]["rank_ic"], result.metrics["lgb"]["rank_ic"])
        self.assertEqual(result.random_state, 42)

    def test_load_latest_model_uses_saved_bundle(self):
        frame = model_trainer.synthetic_training_frame()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = model_trainer.train_stacking_model(frame)
            model_trainer.save_model_bundle(bundle, Path(tmp), version="20990102")

            loaded = engine.load_latest_model(tmp)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["config"]["model_version"], "20990102")
        self.assertIn("feature_columns", loaded["config"])


if __name__ == "__main__":
    unittest.main()

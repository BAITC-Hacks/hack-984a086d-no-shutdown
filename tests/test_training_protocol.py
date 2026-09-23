"""Tests run with unittest as well as pytest; no network or weather required."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from windagent.data import load_hourly
from windagent.model import WindBinCurve, candidate_models, make_features
from windagent.train import train_models

HEADERS = ["Статистическое время", "Средняя скорость ветра(m/s)",
           "Нормализованная активная мощность", "Средняя температура окружающей среды(°C)"]


class ConstantModel:
    def __init__(self, value):
        self.value = value

    def fit(self, x, y):
        return self

    def predict(self, x):
        return np.full(len(x), self.value)


def write_both(path, rows):
    for turbine in (1, 2):
        pd.DataFrame(rows, columns=HEADERS).to_csv(path / f"turbine_{turbine}.csv", index=False)


class DataAndProtocolTests(unittest.TestCase):
    def test_end_timestamp_and_minimum_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            times = pd.date_range("2025-01-01 00:10", periods=6, freq="10min")
            rows = [[str(t), 8, 0.4, -3] for t in times]
            write_both(path, rows)
            frame = load_hourly(path, timestamp_semantics="end")
            self.assertEqual(len(frame), 2)
            self.assertTrue((frame.sample_count == 6).all())
            self.assertTrue((frame.available_at - frame.timestamp == pd.Timedelta(hours=1)).all())
            self.assertEqual(str(frame.timestamp.iloc[0]), "2024-12-31 19:00:00+00:00")
            self.assertEqual(len(load_hourly(path, timestamp_semantics="start")), 0)
            self.assertEqual(len(load_hourly(path, timestamp_semantics="start", min_samples=5)), 2)

    def test_conflicting_duplicates_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_both(path, [["2025-01-01 00:00", 5, 0.2, 1], ["2025-01-01 00:00", 5, 0.8, 1]])
            with self.assertRaisesRegex(ValueError, "conflicting"):
                load_hourly(path)

    def test_holdout_cannot_choose_the_model_and_cutoff_is_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            times = pd.date_range("2025-01-01", "2025-02-28 23:50", freq="10min")
            # Validation prefers low, holdout prefers high. Selection must remain low.
            rows = [[str(t), 7, 0.1 if t < pd.Timestamp("2025-02-15") else 0.9, 2] for t in times]
            write_both(path, rows)
            with patch("windagent.train.candidate_models", lambda: {"low": ConstantModel(0.1), "high": ConstantModel(0.9)}):
                metadata = train_models(path, path / "artifacts", cutoff="2025-02-25T00:00:00+05:00",
                                        holdout_start="2025-02-15T00:00:00+05:00", validation_months=1, folds=1)
            for item in metadata["turbines"].values():
                self.assertEqual(item["selected_candidate"], "low")
                self.assertGreater(item["holdout"]["mae"], 0.79)
                self.assertEqual(item["training_hours"], 55 * 24)
                self.assertEqual(item["training_last_hour_utc"], "2025-02-24T18:00:00Z")
                fold = item["rolling_validation_folds"][0]
                self.assertLess(pd.Timestamp(fold["fit"]["last_hour_utc"]), pd.Timestamp(fold["validation"]["first_hour_utc"]))

    def test_config_rejects_unknown_candidate_and_key(self):
        with self.assertRaises(ValueError):
            candidate_models({"candidates": ["not_a_model"]})
        with self.assertRaises(ValueError):
            candidate_models({"random_split": True})

    def test_curve_is_trained_and_finite(self):
        weather = pd.DataFrame({"timestamp": pd.date_range("2025-01-01", periods=10, freq="h", tz="UTC"),
                                "wind_speed": np.arange(10), "temperature": np.zeros(10)})
        features = make_features(weather)
        y = np.linspace(0, 1, 10)
        model = WindBinCurve(0.25, "median").fit(features, y)
        np.testing.assert_allclose(model.predict(features), y)


if __name__ == "__main__":
    unittest.main()

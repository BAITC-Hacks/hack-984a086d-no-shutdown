import hashlib
import json

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor

from windagent.data import load_hourly
from windagent.model import make_features, predict_power
from windagent import train as train_module


HEADERS = [
    "Статистическое время",
    "Средняя скорость ветра(m/s)",
    "Нормализованная активная мощность",
    "Средняя температура окружающей среды(°C)",
]


class MeanRegressor:
    def fit(self, x, y):
        self.mean_ = float(np.mean(y))
        return self

    def predict(self, x):
        return np.repeat(self.mean_, len(x))


def test_load_hourly_preserves_gaps_and_requires_four_samples(tmp_path):
    rows = []
    for minute in range(0, 60, 10):
        rows.append([f"2025-01-01 00:{minute:02d}:00", 5 + minute / 100, 0.4, 2])
    # Three valid points in the next hour are insufficient; off-grid data does not count.
    rows.extend([
        [f"2025-01-01 01:{minute:02d}:00", 6, 0.5, 3] for minute in (0, 10, 20)
    ])
    rows.append(["2025-01-01 01:30:01", 6, 0.5, 3])
    for turbine in (1, 2):
        pd.DataFrame(rows, columns=HEADERS).to_csv(tmp_path / f"turbine_{turbine}.csv", index=False)

    hourly = load_hourly(tmp_path)
    assert len(hourly) == 2
    assert hourly.groupby("turbine_id").size().to_dict() == {1: 1, 2: 1}
    assert set(hourly.sample_count) == {6}
    assert hourly.timestamp.dt.tz is not None
    assert hourly.attrs["quality_report"]["1"]["off_grid_timestamp_rows"] == 1
    assert hourly.attrs["quality_report"]["1"]["dropped_undercovered_hours"] == 1


def test_make_features_requires_aware_timestamps_and_finite_weather():
    valid = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-01T00:00:00Z"]),
        "wind_speed": [7.0], "temperature": [4.0],
    })
    features = make_features(valid)
    assert list(features.columns) == ["wind_speed", "temperature", "hour_sin", "hour_cos", "year_day_sin", "year_day_cos"]
    assert np.isfinite(features.to_numpy()).all()
    same_instant_other_offset = valid.assign(timestamp=pd.to_datetime(["2025-01-01T05:00:00+05:00"]))
    pd.testing.assert_frame_equal(features, make_features(same_instant_other_offset))
    with pytest.raises(ValueError, match="timezone-aware"):
        make_features(valid.assign(timestamp=pd.to_datetime(["2025-01-01 00:00:00"])))
    with pytest.raises(ValueError, match="finite"):
        make_features(valid.assign(wind_speed=[np.nan]))


def test_predict_power_clips_normalized_output_and_checks_artifact_hash(tmp_path):
    model = DummyRegressor(strategy="constant", constant=1.2).fit([[0], [1]], [1, 1])
    artifact = tmp_path / "turbine_1.joblib"
    joblib.dump({"model": model, "interval_radius": 0.25}, artifact)
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (tmp_path / "metadata.json").write_text(json.dumps({
        "turbines": {"1": {"artifact": artifact.name, "artifact_sha256": artifact_hash}}
    }), encoding="utf-8")
    weather = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=2, freq="h", tz="UTC"),
        "wind_speed": [4.0, 5.0], "temperature": [3.0, 2.0], "source": ["test", "test"],
    })
    result = predict_power(tmp_path, 1, weather)
    assert list(result.source) == ["test", "test"]
    assert result.power.tolist() == [1.0, 1.0]
    assert result.lower.tolist() == [0.75, 0.75]
    assert result.upper.tolist() == [1.0, 1.0]

    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash"):
        predict_power(tmp_path, 1, weather)


def test_train_models_excludes_hours_ending_after_cutoff(tmp_path, monkeypatch):
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)
    local_hours = pd.date_range("2025-01-01 00:00", periods=1312, freq="h")
    for turbine in (1, 2):
        rows = []
        for index, hour in enumerate(local_hours):
            value = 0.2 + (index % 100) / 200 if index < 1300 else 0.95
            for minute in range(0, 60, 10):
                rows.append([
                    (hour + pd.Timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S"),
                    5 + (index % 7) / 10, value, 3,
                ])
        pd.DataFrame(rows, columns=HEADERS).to_csv(raw_dir / f"turbine_{turbine}.csv", index=False)

    monkeypatch.setattr(train_module, "candidate_models", lambda: {"mean_only": MeanRegressor()})
    cutoff = (local_hours[1300].tz_localize("Asia/Almaty")).isoformat()
    artifact_dir = tmp_path / "results" / "artifacts"
    metadata = train_module.train_models(raw_dir, artifact_dir, cutoff=cutoff)

    for turbine in (1, 2):
        item = metadata["turbines"][str(turbine)]
        assert item["training_hours"] == 1300
        assert item["training_last_hour_utc"] == (local_hours[1299].tz_localize("Asia/Almaty").tz_convert("UTC")).isoformat().replace("+00:00", "Z")
        assert item["chronological_split"]["holdout"]["first_hour_utc"] == (local_hours[1040].tz_localize("Asia/Almaty").tz_convert("UTC")).isoformat().replace("+00:00", "Z")
        assert item["candidate_validation_metrics"]["mean_only"]["n"] == 195
        fitted = joblib.load(artifact_dir / item["artifact"])["model"]
        assert fitted.mean_ < 0.95

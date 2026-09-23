"""Deterministic conditional power models and inference helpers."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


FEATURE_SCHEMA = [
    "wind_speed", "temperature", "hour_sin", "hour_cos", "year_day_sin", "year_day_cos"
]
MODEL_VERSION = "conditional-power-v1"


def make_features(weather: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "wind_speed", "temperature"}
    missing = required.difference(weather.columns)
    if missing:
        raise ValueError(f"weather is missing columns: {sorted(missing)}")
    timestamps = pd.to_datetime(weather["timestamp"], errors="raise")
    if timestamps.dt.tz is None:
        raise ValueError("weather timestamps must be timezone-aware")
    timestamps = timestamps.dt.tz_convert("UTC")
    wind = pd.to_numeric(weather["wind_speed"], errors="coerce").to_numpy(dtype=float)
    temp = pd.to_numeric(weather["temperature"], errors="coerce").to_numpy(dtype=float)
    if not (np.isfinite(wind).all() and np.isfinite(temp).all()):
        raise ValueError("weather values must be finite")
    hours = timestamps.dt.hour.to_numpy() + timestamps.dt.minute.to_numpy() / 60.0
    day_fraction = timestamps.dt.dayofyear.to_numpy() - 1 + hours / 24.0
    return pd.DataFrame({
        "wind_speed": wind,
        "temperature": temp,
        "hour_sin": np.sin(2 * np.pi * hours / 24),
        "hour_cos": np.cos(2 * np.pi * hours / 24),
        "year_day_sin": np.sin(2 * np.pi * day_fraction / 365.2425),
        "year_day_cos": np.cos(2 * np.pi * day_fraction / 365.2425),
    }, index=weather.index)[FEATURE_SCHEMA]


class WindBinCurve:
    """Small interpolated wind-speed bin mean baseline."""

    def __init__(self, bin_width: float = 1.0):
        self.bin_width = bin_width

    def fit(self, x: pd.DataFrame, y: np.ndarray) -> "WindBinCurve":
        wind = x["wind_speed"].to_numpy()
        edges = np.arange(0, max(101, np.ceil(wind.max() + self.bin_width)), self.bin_width)
        index = np.minimum(np.digitize(wind, edges) - 1, len(edges) - 2)
        centers: list[float] = []
        means: list[float] = []
        for i in np.unique(index):
            mask = index == i
            centers.append(float(np.mean(wind[mask])))
            means.append(float(np.mean(y[mask])))
        self.centers_ = np.asarray(centers)
        self.means_ = np.asarray(means)
        return self

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return np.interp(x["wind_speed"].to_numpy(), self.centers_, self.means_,
                         left=self.means_[0], right=self.means_[-1])


def candidate_models() -> dict[str, Any]:
    return {
        "hist_gradient_boosting": HistGradientBoostingRegressor(
            max_iter=160, learning_rate=0.06, max_leaf_nodes=23,
            min_samples_leaf=35, l2_regularization=1.0, random_state=17,
        ),
        "wind_bin_curve": WindBinCurve(),
    }


def regression_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float | int]:
    actual = np.asarray(actual, dtype=float)
    predicted = np.clip(np.asarray(predicted, dtype=float), 0.0, 1.0)
    return {
        "n": int(len(actual)),
        "mae": float(mean_absolute_error(actual, predicted)),
        "rmse": float(mean_squared_error(actual, predicted) ** 0.5),
        "bias": float(np.mean(predicted - actual)),
        "r2": float(r2_score(actual, predicted)) if len(actual) > 1 else float("nan"),
    }


def _metadata(artifact_dir: Path) -> dict[str, Any]:
    path = Path(artifact_dir) / "metadata.json"
    if not path.is_file():
        raise FileNotFoundError(f"model metadata not found: {path}")
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def predict_power(artifact_dir: Path, turbine_id: int, weather: pd.DataFrame) -> pd.DataFrame:
    """Predict normalized power and pretest conditional residual intervals.

    Interval bounds describe validation residual spread conditional on this
    model/weather feature set; they do not include weather forecast uncertainty.
    Only artifact names recorded in metadata are loaded, and they must resolve
    directly inside ``artifact_dir``.
    """
    metadata = _metadata(Path(artifact_dir))
    turbine = metadata.get("turbines", {}).get(str(int(turbine_id)))
    if turbine is None:
        raise ValueError(f"no model for turbine {turbine_id}")
    filename = turbine["artifact"]
    artifact_path = (Path(artifact_dir) / filename).resolve()
    if artifact_path.parent != Path(artifact_dir).resolve():
        raise ValueError("artifact path escapes artifact directory")
    if not artifact_path.is_file():
        raise FileNotFoundError(f"model artifact not found: {artifact_path}")
    expected_hash = turbine.get("artifact_sha256")
    if expected_hash:
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if digest != expected_hash:
            raise ValueError("model artifact hash does not match metadata")
    packaged = joblib.load(artifact_path)
    features = make_features(weather)
    predicted = np.clip(packaged["model"].predict(features), 0.0, 1.0)
    radius = float(packaged["interval_radius"])
    result = weather.copy()
    result["power"] = predicted
    result["lower"] = np.clip(predicted - radius, 0.0, 1.0)
    result["upper"] = np.clip(predicted + radius, 0.0, 1.0)
    return result

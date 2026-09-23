"""Chronological training, candidate selection, artifact writing and reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn

from .data import load_hourly, sha256_file
from .model import (
    FEATURE_SCHEMA,
    MODEL_VERSION,
    candidate_models,
    make_features,
    regression_metrics,
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _iso(value: pd.Timestamp) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _data_profile(hourly: pd.DataFrame, quality: dict[str, Any], timezone: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "timezone_assumption": timezone,
        "hourly_interval_semantics": "UTC interval-start timestamp; mean available at interval end",
        "minimum_samples_per_hour": 4,
        "turbines": {},
    }
    for turbine_id, group in hourly.groupby("turbine_id"):
        times = group["timestamp"].sort_values()
        gaps = times.diff().dropna().dt.total_seconds().div(3600)
        result["turbines"][str(int(turbine_id))] = {
            **quality[str(int(turbine_id))],
            "first_hour_utc": _iso(times.min()),
            "last_hour_utc": _iso(times.max()),
            "observed_hour_span": int((times.max() - times.min()).total_seconds() / 3600) + 1,
            "missing_hours_inside_span": int((times.max() - times.min()).total_seconds() / 3600) + 1 - int(len(group)),
            "first_local_timestamp": quality[str(int(turbine_id))]["first_valid_local_timestamp"],
            "last_local_timestamp": quality[str(int(turbine_id))]["last_valid_local_timestamp"],
            "largest_gap_hours": int(gaps.max()) if len(gaps) else 0,
            "median_samples_per_hour": float(group["sample_count"].median()),
            "wind_speed_range_m_s": [float(group.wind_speed.min()), float(group.wind_speed.max())],
            "temperature_range_c": [float(group.temperature.min()), float(group.temperature.max())],
            "normalized_power_range": [float(group.power.min()), float(group.power.max())],
        }
    return result


def _fit_eval(name: str, x_train: pd.DataFrame, y_train: np.ndarray,
              x_eval: pd.DataFrame) -> tuple[Any, np.ndarray]:
    model = candidate_models()[name]
    model.fit(x_train, y_train)
    return model, np.clip(model.predict(x_eval), 0.0, 1.0)


def train_models(
    data_dir: Path,
    artifact_dir: Path,
    cutoff: str = "2026-02-01T00:00:00+05:00",
    timezone: str = "Asia/Almaty",
) -> dict[str, Any]:
    """Train each turbine independently with validation-only model selection.

    Candidate choice uses the middle chronological validation block. The later
    diagnostic block is scored once by models fitted only on data before it.
    Production artifacts are refit on every eligible pre-cutoff hour afterward.
    """
    data_dir, artifact_dir = Path(data_dir), Path(artifact_dir)
    cutoff_ts = pd.Timestamp(cutoff)
    if cutoff_ts.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    cutoff_utc = cutoff_ts.tz_convert("UTC")
    reports_dir = artifact_dir.parent / "reports"

    all_hourly = load_hourly(data_dir, timezone=timezone)
    quality = all_hourly.attrs["quality_report"]
    profile = _data_profile(all_hourly, quality, timezone)
    _write_json(reports_dir / "dataset_profile.json", profile)

    # The right edge of an hourly bin is its availability time.
    eligible = all_hourly.loc[all_hourly["timestamp"] + pd.Timedelta(hours=1) <= cutoff_utc].copy()
    if eligible.empty:
        raise ValueError("no complete hourly observations are available before cutoff")

    turbine_metadata: dict[str, Any] = {}
    all_metrics: dict[str, Any] = {}
    report_rows: list[str] = []
    for turbine_id, group in eligible.groupby("turbine_id", sort=True):
        group = group.sort_values("timestamp").reset_index(drop=True)
        n = len(group)
        train_end, validation_end = int(n * 0.65), int(n * 0.80)
        if train_end < 300 or validation_end - train_end < 100 or n - validation_end < 100:
            raise ValueError(f"not enough chronological data for turbine {turbine_id}: {n} hours")

        feature_rows = make_features(group[["timestamp", "wind_speed", "temperature"]])
        y = group["power"].to_numpy(dtype=float)
        candidates: dict[str, Any] = {}
        for name in candidate_models():
            _, validation_pred = _fit_eval(name, feature_rows.iloc[:train_end], y[:train_end],
                                           feature_rows.iloc[train_end:validation_end])
            candidates[name] = {
                "validation": regression_metrics(y[train_end:validation_end], validation_pred),
                "validation_predictions": validation_pred,
            }
        selected_name = min(candidates, key=lambda name: candidates[name]["validation"]["mae"])
        validation_actual = y[train_end:validation_end]
        validation_pred = candidates[selected_name]["validation_predictions"]
        residual_radius = float(np.quantile(np.abs(validation_actual - validation_pred), 0.90, method="higher"))

        # Untouched diagnostic holdout: refit selected and baseline models through validation only.
        test_start = validation_end
        test_metrics: dict[str, Any] = {}
        test_predictions_by_model: dict[str, np.ndarray] = {}
        for name in candidate_models():
            _, test_pred = _fit_eval(name, feature_rows.iloc[:test_start], y[:test_start],
                                     feature_rows.iloc[test_start:])
            test_predictions_by_model[name] = test_pred
            test_metrics[name] = regression_metrics(y[test_start:], test_pred)
        chosen_test_metrics = test_metrics[selected_name]

        production_model = candidate_models()[selected_name]
        production_model.fit(feature_rows, y)
        artifact_name = f"turbine_{int(turbine_id)}.joblib"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / artifact_name
        joblib.dump({
            "model": production_model,
            "model_version": MODEL_VERSION,
            "selected_candidate": selected_name,
            "feature_schema": FEATURE_SCHEMA,
            "interval_radius": residual_radius,
            "interval_quantile": 0.90,
            "interval_calibration": "absolute validation residuals; conditional on observed weather features",
        }, artifact_path, compress=3)
        turbine_metadata[str(int(turbine_id))] = {
            "artifact": artifact_name,
            "artifact_sha256": sha256_file(artifact_path),
            "selected_candidate": selected_name,
            "training_hours": int(n),
            "training_first_hour_utc": _iso(group.timestamp.min()),
            "training_last_hour_utc": _iso(group.timestamp.max()),
            "chronological_split": {
                "fit": {
                    "row_count": int(train_end),
                    "first_hour_utc": _iso(group.timestamp.iloc[0]),
                    "last_hour_utc": _iso(group.timestamp.iloc[train_end - 1]),
                },
                "validation": {
                    "row_count": int(validation_end - train_end),
                    "first_hour_utc": _iso(group.timestamp.iloc[train_end]),
                    "last_hour_utc": _iso(group.timestamp.iloc[validation_end - 1]),
                },
                "holdout": {
                    "row_count": int(n - validation_end),
                    "first_hour_utc": _iso(group.timestamp.iloc[validation_end]),
                    "last_hour_utc": _iso(group.timestamp.iloc[n - 1]),
                },
                "selection_rule": "minimum validation MAE; holdout metrics are diagnostic only",
            },
            "candidate_validation_metrics": {
                name: item["validation"] for name, item in candidates.items()
            },
            "selection_justification": (
                f"Selected {selected_name}: it had the lowest MAE on the chronological validation block; "
                "the later holdout was not used for selection."
            ),
            "validation": candidates[selected_name]["validation"],
            "holdout": chosen_test_metrics,
            "holdout_candidates": test_metrics,
            "conditional_interval": {
                "quantile": 0.90,
                "symmetric_radius": residual_radius,
                "validation_empirical_coverage": float(np.mean(np.abs(validation_actual - validation_pred) <= residual_radius)),
            },
        }
        all_metrics[str(int(turbine_id))] = {
            "validation": candidates[selected_name]["validation"],
            "holdout": chosen_test_metrics,
            "holdout_candidates": test_metrics,
        }
        report_rows.append(
            f"| {int(turbine_id)} | {n:,} | {selected_name} | {candidates[selected_name]['validation']['mae']:.4f} | "
            f"{chosen_test_metrics['mae']:.4f} | {chosen_test_metrics['rmse']:.4f} | {chosen_test_metrics['r2']:.3f} |"
        )

    metadata: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "model_available_at": _iso(cutoff_utc),
        "model_available_at_local": cutoff_ts.isoformat(),
        "timezone": timezone,
        "feature_schema": FEATURE_SCHEMA,
        "input_files": ["turbine_1.csv", "turbine_2.csv"],
        "library_versions": {
            "pandas": pd.__version__, "numpy": np.__version__, "scikit_learn": sklearn.__version__,
            "joblib": joblib.__version__,
        },
        "input_hashes": {key: row["raw_sha256"] for key, row in quality.items()},
        "turbines": turbine_metadata,
        "metrics": all_metrics,
        "limitations": [
            "February 2026 turbine observations are absent; no February actual-power score is available.",
            "Validation and holdout use observed wind and temperature, so they are conditional weather-to-power diagnostics, not day-ahead forecast skill.",
            "Prediction intervals calibrate power-model residuals conditional on supplied weather and exclude weather-forecast uncertainty.",
            "Input timestamps were treated as Asia/Almaty local time; normalized output is not kW or kWh.",
        ],
    }
    _write_json(artifact_dir / "metadata.json", metadata)
    report = [
        "# Wind power model training report", "",
        f"Model version: `{MODEL_VERSION}`. Frozen availability cutoff: `{cutoff_ts.isoformat()}` ({_iso(cutoff_utc)} UTC).",
        "", "## Chronological diagnostics", "",
        "Candidates were selected using a 65%/15% chronological fit/validation split. The final 20% was held out from selection; diagnostic models were fitted only on earlier observations. Production artifacts were then refit on all eligible hours before the cutoff.",
        "", "| Turbine | Eligible hours | Selected model | Validation MAE | Holdout MAE | Holdout RMSE | Holdout R² |",
        "|---:|---:|---|---:|---:|---:|---:|", *report_rows,
        "", "These scores use observed wind speed and temperature. They measure the conditional power curve/model only, not 24–48-hour forecast accuracy. February actuals are unavailable.",
        "", "## Data quality", "", "See `dataset_profile.json` for raw-row counts, invalid rows, duplicates, undercovered hours, gaps, value ranges, and source hashes.",
        "", "## Interval meaning", "", "Lower and upper bounds use the 90th percentile absolute residual from the pre-holdout validation block, applied symmetrically and clipped to normalized power [0, 1]. They are conditional on the weather features supplied to the model and do not include uncertainty in the weather forecast.",
        "", "## Reproducibility", "", "Training uses deterministic scikit-learn estimators with fixed random state, per-source SHA-256 hashes, and a separate artifact for each turbine. The input data ends at 2026-01-31 23:50 local time; complete hourly means are required before the cutoff.", "",
    ]
    (reports_dir / "training_report.md").write_text("\n".join(report), encoding="utf-8")
    return metadata

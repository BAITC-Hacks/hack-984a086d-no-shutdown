"""Reproducible rolling validation, untouched final holdout and production fit."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from threadpoolctl import threadpool_limits

from .data import load_hourly, sha256_file
from .model import FEATURE_SCHEMA, MODEL_VERSION, candidate_models, make_features, regression_metrics


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _iso(value: pd.Timestamp) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _data_profile(hourly: pd.DataFrame, quality: dict[str, Any], timezone: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "timezone_assumption": timezone,
        "timestamp_semantics_assumption": hourly.attrs["timestamp_semantics_assumption"],
        "hourly_interval_semantics": "UTC interval start; available at interval end (zero telemetry delay assumed)",
        "minimum_samples_per_hour": hourly.attrs["minimum_samples_per_hour"], "turbines": {},
    }
    for turbine_id, group in hourly.groupby("turbine_id"):
        times = group.timestamp.sort_values()
        gaps = times.diff().dropna().dt.total_seconds().div(3600)
        span = int((times.max() - times.min()).total_seconds() / 3600) + 1
        result["turbines"][str(int(turbine_id))] = {
            **quality[str(int(turbine_id))],
            "first_hour_utc": _iso(times.min()), "last_hour_utc": _iso(times.max()),
            "observed_hour_span": span, "missing_hours_inside_span": span - int(len(group)),
            "largest_gap_hours": int(gaps.max()) if len(gaps) else 0,
            "median_samples_per_hour": float(group.sample_count.median()),
            "wind_speed_range_m_s": [float(group.wind_speed.min()), float(group.wind_speed.max())],
            "temperature_range_c": [float(group.temperature.min()), float(group.temperature.max())],
            "normalized_power_range": [float(group.power.min()), float(group.power.max())],
        }
    return result


def _bounds(group: pd.DataFrame) -> dict[str, Any]:
    return {"row_count": len(group), "first_hour_utc": _iso(group.timestamp.min()),
            "last_hour_utc": _iso(group.timestamp.max())}


def train_models(data_dir: Path, artifact_dir: Path,
                 cutoff: str = "2026-02-01T00:00:00+05:00", timezone: str = "Asia/Almaty", *,
                 timestamp_semantics: str = "start", min_samples: int = 6,
                 holdout_start: str | None = None, validation_months: int = 6,
                 folds: int = 3, model_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Select on rolling past-only folds; report a final month once, then refit.

    Default: Jul–Aug, Sep–Oct, Nov–Dec 2025 selection; January 2026 final
    conditional holdout. Each fold refits on earlier data. Observed weather
    is used, so this is not a measure of day-ahead weather forecast skill.
    """
    if folds < 1 or validation_months < folds or validation_months % folds:
        raise ValueError("validation_months must be positive and divisible by folds")
    cutoff_ts = pd.Timestamp(cutoff)
    if cutoff_ts.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    cutoff_utc = cutoff_ts.tz_convert("UTC")
    cutoff_local = cutoff_ts.tz_convert(timezone)
    if holdout_start is None:
        holdout_ts = cutoff_local.normalize().replace(day=1) - pd.DateOffset(months=1)
    else:
        holdout_ts = pd.Timestamp(holdout_start)
        if holdout_ts.tzinfo is None:
            raise ValueError("holdout_start must be timezone-aware")
        holdout_ts = holdout_ts.tz_convert(timezone)
    if holdout_ts >= cutoff_ts:
        raise ValueError("holdout_start must precede cutoff")
    validation_start = holdout_ts - pd.DateOffset(months=validation_months)
    boundaries = [validation_start + pd.DateOffset(months=i * (validation_months // folds))
                  for i in range(folds + 1)]
    factories = lambda: candidate_models(model_config) if model_config else candidate_models()
    names = list(factories())
    artifact_dir, data_dir = Path(artifact_dir), Path(data_dir)
    reports_dir = artifact_dir.parent / "reports"
    all_hourly = load_hourly(data_dir, timezone, timestamp_semantics=timestamp_semantics, min_samples=min_samples)
    quality = all_hourly.attrs["quality_report"]
    _write_json(reports_dir / "dataset_profile.json", _data_profile(all_hourly, quality, timezone))
    eligible = all_hourly.loc[all_hourly.available_at <= cutoff_utc].copy()
    if set(eligible.turbine_id) != {1, 2}:
        raise ValueError("both turbines need eligible complete hourly data before cutoff")
    turbine_metadata: dict[str, Any] = {}
    all_metrics: dict[str, Any] = {}
    comparison_rows: list[str] = []
    tradeoffs: list[str] = []
    saved_predictions: list[pd.DataFrame] = []
    with threadpool_limits(limits=2):
        for turbine_id, group in eligible.groupby("turbine_id", sort=True):
            group = group.sort_values("timestamp").reset_index(drop=True)
            features = make_features(group)
            y = group.power.to_numpy(dtype=float)
            fold_records, cv_predictions, cv_actuals = [], {name: [] for name in names}, []
            for start, end in zip(boundaries[:-1], boundaries[1:]):
                fit_mask = group.available_at <= start.tz_convert("UTC")
                eval_mask = (group.timestamp >= start.tz_convert("UTC")) & (group.available_at <= end.tz_convert("UTC"))
                if fit_mask.sum() < 300 or eval_mask.sum() < 30:
                    raise ValueError(f"T{turbine_id}: insufficient data for fold {start}–{end}; adjust validation dates")
                record: dict[str, Any] = {"fit": _bounds(group.loc[fit_mask]), "validation": _bounds(group.loc[eval_mask]), "candidates": {}}
                cv_actuals.append(y[eval_mask])
                for name, model in factories().items():
                    model.fit(features.loc[fit_mask], y[fit_mask])
                    predicted = np.clip(model.predict(features.loc[eval_mask]), 0, 1)
                    cv_predictions[name].append(predicted)
                    record["candidates"][name] = regression_metrics(y[eval_mask], predicted)
                fold_records.append(record)
            cv_y = np.concatenate(cv_actuals)
            validation_metrics = {}
            for name in names:
                validation_metrics[name] = regression_metrics(cv_y, np.concatenate(cv_predictions[name]))
                validation_metrics[name]["mean_fold_mae"] = float(np.mean([row["candidates"][name]["mae"] for row in fold_records]))
            selected = min(names, key=lambda name: validation_metrics[name]["mean_fold_mae"])
            residuals = np.abs(cv_y - np.concatenate(cv_predictions[selected]))
            radius = float(np.quantile(residuals, 0.90, method="higher"))
            fit_mask = group.available_at <= holdout_ts.tz_convert("UTC")
            test_mask = group.timestamp >= holdout_ts.tz_convert("UTC")
            if test_mask.sum() < 30:
                raise ValueError(f"T{turbine_id}: fewer than 30 holdout hours")
            # Score the already selected winner and fixed references; never select on holdout.
            diagnostic_names = list(dict.fromkeys([selected] + [n for n in ("hist_gradient_boosting", "wind_bin_curve") if n in names]))
            holdout_metrics = {}
            diagnostic = group.loc[test_mask, ["turbine_id", "timestamp", "power"]].rename(columns={"power": "actual"}).copy()
            for name in diagnostic_names:
                model = factories()[name]
                model.fit(features.loc[fit_mask], y[fit_mask])
                predicted = np.clip(model.predict(features.loc[test_mask]), 0, 1)
                holdout_metrics[name] = regression_metrics(y[test_mask], predicted)
                diagnostic[name] = predicted
            diagnostic["selected_candidate"] = selected
            saved_predictions.append(diagnostic)
            selected_test = holdout_metrics[selected]
            production = factories()[selected]
            production.fit(features, y)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            filename = f"turbine_{int(turbine_id)}.joblib"
            artifact_path = artifact_dir / filename
            joblib.dump({"model": production, "model_version": MODEL_VERSION,
                         "selected_candidate": selected, "feature_schema": FEATURE_SCHEMA,
                         "interval_radius": radius, "interval_quantile": 0.90,
                         "interval_calibration": "rolling validation absolute residuals; observed weather conditional"},
                        artifact_path, compress=3)
            turbine_metadata[str(int(turbine_id))] = {
                "artifact": filename, "artifact_sha256": sha256_file(artifact_path),
                "selected_candidate": selected, "training_hours": int(len(group)),
                "training_first_hour_utc": _iso(group.timestamp.min()),
                "training_last_hour_utc": _iso(group.timestamp.max()),
                "chronological_split": {
                    "fit": _bounds(group.loc[group.available_at <= boundaries[0].tz_convert("UTC")]),
                    "validation": _bounds(group.loc[(group.timestamp >= boundaries[0].tz_convert("UTC")) & fit_mask]),
                    "holdout": _bounds(group.loc[test_mask]),
                    "selection_rule": "minimum mean rolling-fold MAE; final holdout not used for selection",
                },
                "rolling_validation_folds": fold_records,
                "candidate_validation_metrics": validation_metrics,
                "selection_justification": f"{selected}: lowest mean MAE across {folds} chronological folds, before final holdout.",
                "validation": validation_metrics[selected], "holdout": selected_test,
                "holdout_candidates": holdout_metrics,
                "conditional_interval": {"quantile": 0.90, "symmetric_radius": radius,
                                         "validation_empirical_coverage": float(np.mean(residuals <= radius)),
                                         "holdout_empirical_coverage": float(np.mean(np.abs(diagnostic.actual - diagnostic[selected]) <= radius))},
                "training_weather_ranges": {name: [float(group[name].min()), float(group[name].max())] for name in ("wind_speed", "temperature")},
            }
            all_metrics[str(int(turbine_id))] = {"validation": validation_metrics[selected], "holdout": selected_test, "holdout_candidates": holdout_metrics}
            baseline = holdout_metrics.get("hist_gradient_boosting", selected_test)
            if selected_test["rmse"] > baseline["rmse"]:
                tradeoffs.append(f"T{int(turbine_id)}: MAE {selected_test['mae']:.5f} против {baseline['mae']:.5f} у исходного HGB; RMSE немного хуже ({selected_test['rmse']:.5f} против {baseline['rmse']:.5f}). Критерием выбора был MAE; улучшение по всем метрикам не заявляется.")
            comparison_rows.append(f"| T{int(turbine_id)} | {len(group):,} | {selected} | {validation_metrics[selected]['mean_fold_mae']:.5f} | {selected_test['mae']:.5f} | {baseline['mae']:.5f} | {selected_test['rmse']:.5f} |")
    pd.concat(saved_predictions).to_csv(reports_dir / "conditional_holdout_predictions.csv", index=False)
    metadata: dict[str, Any] = {
        "model_version": MODEL_VERSION, "model_available_at": _iso(cutoff_utc),
        "model_available_at_local": cutoff_ts.isoformat(), "timezone": timezone,
        "feature_schema": FEATURE_SCHEMA, "input_files": ["turbine_1.csv", "turbine_2.csv"],
        "training_config": {"timestamp_semantics": timestamp_semantics, "min_samples": min_samples,
                            "validation_months": validation_months, "folds": folds,
                            "holdout_start": holdout_ts.isoformat(), "model_config": model_config or {}},
        "library_versions": {"pandas": pd.__version__, "numpy": np.__version__, "scikit_learn": sklearn.__version__, "joblib": joblib.__version__},
        "input_hashes": {key: row["raw_sha256"] for key, row in quality.items()},
        "turbines": turbine_metadata, "metrics": all_metrics,
        "limitations": [
            "No February 2026 actual turbine power was supplied; February accuracy cannot be measured.",
            "Scores use observed weather, not forecast weather; they are conditional power-model diagnostics, not 24–48 h forecast skill.",
            "Residual bands exclude weather uncertainty and have no guaranteed coverage for operational forecasts.",
            f"CSV timezone={timezone}, timestamp={timestamp_semantics}, telemetry delay=0 are explicit unconfirmed assumptions.",
            "Power normalization formula and rated capacities are unknown; predictions are dimensionless, not MW/MWh.",
        ],
    }
    _write_json(artifact_dir / "metadata.json", metadata)
    report = ["# Обучение модели мощности", "",
              f"Версия `{MODEL_VERSION}`. Данные ограничены `{cutoff_ts.isoformat()}`. Измерений в час: минимум {min_samples}/6.", "",
              "## Проверка без перемешивания времени", "",
              f"Выбор модели: {folds} последовательных окон между {validation_start.date()} и {holdout_ts.date()}. Каждое окно использует только предшествующие наблюдения. Критерий — среднее MAE окон.", "",
              f"Финальная проверка: {holdout_ts.date()} — {cutoff_local.date()} (правая граница исключена). Этот период не используется для выбора модели. После оценки выбранная модель переобучена на всех доступных данных для рабочего прогноза.", "",
              "| Турбина | Часов обучения | Выбранная модель | CV MAE | Holdout MAE | Исходный HGB MAE | Holdout RMSE |",
              "|---|---:|---|---:|---:|---:|---:|", *comparison_rows, "",
              "MAE/RMSE — в единицах нормализованной мощности. Это ошибки при известных фактических ветре и температуре; они НЕ измеряют точность прогноза на 24–48 часов. Оба алгоритма сравниваются на одинаковых полных часах и временных границах.", "",
              *tradeoffs, "",
              "Все кандидаты и метрики окон: `artifacts/metadata.json`. Индивидуальные прогнозы: `reports/conditional_holdout_predictions.csv`. Качество CSV: `reports/dataset_profile.json`.", "",
              "## Интервал и ограничения", "",
              "Полоса — 90-й перцентиль абсолютных ошибок выбранной модели на скользящих окнах; это условный разброс ошибки мощности при заданной погоде. Он не включает неопределённость будущей погоды и не гарантирует 90% покрытия будущих наблюдений.", "",
              *[f"- {item}" for item in metadata["limitations"]], "",
              "Настройка и воспроизведение: `docs/TUNING.md`. random_state=17 и SHA-256 исходных данных/артефактов записаны в метаданных."]
    (reports_dir / "training_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return metadata

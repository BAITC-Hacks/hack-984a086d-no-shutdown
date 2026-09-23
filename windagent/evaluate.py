"""Rolling-origin weather replay; provenance status travels with every score."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .data import load_hourly
from .model import predict_power, regression_metrics
from .train import train_models
from .weather import fetch_weather


def run_january_backtest(
    data_dir: Path,
    artifact_workspace: Path,
    weather_cache_dir: Path,
    report_path: Path,
    timezone: str = "Asia/Almaty",
    first_origin: str = "2026-01-01T00:00:00+05:00",
    origin_count: int = 7,
) -> dict[str, Any]:
    """Train at Jan 1, replay consecutive daily 48h origins, and score actuals.

    Every power prediction uses an individual archived ECMWF run that passed
    the configured date gate. A date gate alone does not prove that hindcasts
    were operationally published in the past; provenance remains unverified.
    Failures remain visible in the output and missing actual hours are excluded.
    Artifacts/reports are kept under ``artifact_workspace``; production artifacts
    and the primary conditional model report are untouched.
    """
    if origin_count < 1:
        raise ValueError("origin_count must be positive")
    start = pd.Timestamp(first_origin)
    if start.tzinfo is None:
        raise ValueError("first_origin must be timezone-aware")
    cutoff = start.isoformat()
    workspace = Path(artifact_workspace)
    artifacts = workspace / "artifacts"
    metadata = train_models(data_dir, artifacts, cutoff=cutoff, timezone=timezone)
    actuals = load_hourly(data_dir, timezone=timezone).set_index(["turbine_id", "timestamp"])
    prediction_frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    origin_records: list[dict[str, Any]] = []
    origins = [start + pd.Timedelta(days=i) for i in range(origin_count)]

    for origin in origins:
        origin_record: dict[str, Any] = {"origin": origin.isoformat(), "turbines": {}}
        for turbine_id in (1, 2):
            try:
                weather = fetch_weather(
                    turbine_id, origin, 48, Path(weather_cache_dir), refresh=False
                )
                forecast = predict_power(artifacts, turbine_id, weather)
                truth = actuals.loc[turbine_id].reset_index()[["timestamp", "power"]]
                joined = forecast.merge(truth, on="timestamp", suffixes=("_predicted", "_actual"))
                known = truth.loc[truth["timestamp"] + pd.Timedelta(hours=1) <= origin.tz_convert("UTC")]
                if known.empty:
                    raise ValueError(f"no complete observed power value was available by origin {origin}")
                stale = known.sort_values("timestamp").iloc[-1]
                joined["turbine_id"] = turbine_id
                joined["origin"] = origin.isoformat()
                joined["lead_hour"] = ((joined["timestamp"] - origin.tz_convert("UTC")).dt.total_seconds() / 3600).astype(int)
                joined["lead_bucket"] = joined["lead_hour"].map(lambda lead: "0-23" if lead < 24 else "24-47")
                joined["initialized_at"] = weather.initialized_at.iloc[0].isoformat()
                joined["weather_source_hash"] = str(weather.source_hash.iloc[0])
                joined["persistence_predicted"] = float(stale["power"])
                joined["persistence_stale_timestamp"] = stale["timestamp"].isoformat()
                joined["persistence_stale_age_hours"] = float((origin.tz_convert("UTC") - (stale["timestamp"] + pd.Timedelta(hours=1))).total_seconds() / 3600)
                prediction_frames.append(joined)
                origin_record["turbines"][str(turbine_id)] = {
                    "status": "scored", "weather_run_initialized_at": weather.initialized_at.iloc[0].isoformat(),
                    "weather_source_hash": str(weather.source_hash.iloc[0]),
                    "as_of_verified": bool(weather["as_of_verified"].iloc[0]) if "as_of_verified" in weather else False,
                    "provenance_status": str(weather["provenance_status"].iloc[0]) if "provenance_status" in weather else "unverified_hindcast",
                    "requested_weather_hours": 48,
                    "matched_actual_hours": int(len(joined)),
                    "missing_actual_hours": int(48 - len(joined)),
                }
            except Exception as exc:  # recorded so unavailable archived runs cannot look like success
                failure = {
                    "origin": origin.isoformat(), "turbine_id": turbine_id,
                    "error_type": type(exc).__name__, "error": str(exc),
                }
                failures.append(failure)
                origin_record["turbines"][str(turbine_id)] = {"status": "failed", **failure}
        origin_record["status"] = "complete" if all(
            origin_record["turbines"].get(str(t), {}).get("status") == "scored" for t in (1, 2)
        ) else "partial_or_failed"
        origin_records.append(origin_record)

    all_rows = pd.concat(prediction_frames, ignore_index=True) if prediction_frames else pd.DataFrame()
    metrics: dict[str, Any] = {}
    if not all_rows.empty:
        for (turbine_id, lead_bucket), group in all_rows.groupby(["turbine_id", "lead_bucket"], sort=True):
            metrics.setdefault(str(int(turbine_id)), {})[lead_bucket] = regression_metrics(
                group["power_actual"].to_numpy(), group["power_predicted"].to_numpy()
            )
            metrics[str(int(turbine_id))][lead_bucket]["persistence_baseline"] = regression_metrics(
                group["power_actual"].to_numpy(), group["persistence_predicted"].to_numpy()
            )
        for lead_bucket, group in all_rows.groupby("lead_bucket", sort=True):
            farm = group.groupby(["origin", "timestamp"], as_index=False)[
                ["power_actual", "power_predicted", "persistence_predicted"]
            ].mean()
            # Only score equal-weight farm proxy where both turbines contributed.
            complete_keys = group.groupby(["origin", "timestamp"])["turbine_id"].nunique()
            complete_keys = complete_keys[complete_keys == 2].index
            farm = farm.set_index(["origin", "timestamp"]).loc[complete_keys].reset_index()
            if farm.empty:
                continue
            metrics.setdefault("farm_equal_weight_proxy", {})[lead_bucket] = regression_metrics(
                farm["power_actual"].to_numpy(), farm["power_predicted"].to_numpy()
            )
            metrics["farm_equal_weight_proxy"][lead_bucket]["persistence_baseline"] = regression_metrics(
                farm["power_actual"].to_numpy(), farm["persistence_predicted"].to_numpy()
            )

    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    predictions_filename = report_path.with_name("january_backtest_predictions.csv").name
    if not all_rows.empty:
        all_rows[[
            "origin", "timestamp", "lead_hour", "lead_bucket", "turbine_id", "power_actual",
            "power_predicted", "persistence_predicted", "persistence_stale_timestamp",
            "persistence_stale_age_hours", "initialized_at", "weather_source_hash",
        ]].to_csv(report_path.with_name(predictions_filename), index=False)
    report: dict[str, Any] = {
        "diagnostic": "unverified_hindcast_weather_rolling_origin",
        "as_of_verified": False,
        "provenance_status": "unverified_hindcast",
        "model_available_at": metadata["model_available_at"],
        "training_cutoff_local": cutoff,
        "training_artifacts": "artifact_workspace/artifacts (separate from production)",
        "origins_requested": [origin.isoformat() for origin in origins],
        "origin_records": origin_records,
        "origins_completed": int(sum(record["status"] == "complete" for record in origin_records)),
        "origins_with_full_actual_coverage": int(sum(
            record["status"] == "complete" and all(
                record["turbines"][str(t)]["matched_actual_hours"] == 48 for t in (1, 2)
            ) for record in origin_records
        )),
        "successful_origin_turbine_pairs": int(len(prediction_frames)),
        "expected_weather_hours_for_successful_pairs": int(48 * len(prediction_frames)),
        "matched_actual_rows": int(len(all_rows)),
        "missing_actual_rows_for_successful_pairs": int(48 * len(prediction_frames) - len(all_rows)),
        "failures": failures,
        "metrics_by_turbine_and_lead": metrics,
        "row_count": int(len(all_rows)),
        "predictions_csv": predictions_filename if not all_rows.empty else None,
        "caveats": [
            "Cached provider data is labeled hindcast. Initialization plus assumed latency passes the configured date gate, but historical operational publication is not verified; this is not proven leakage-free day-ahead skill.",
            "The Jan 1 00:00 local model cutoff precedes every scored origin; observations after the cutoff are excluded from training.",
            "Origins overlap, so the lead-bucket sample rows are correlated and are not independent trials.",
            "Persistence baseline carries forward the latest complete hourly power value available at each origin; stale observation time and age are included per prediction.",
            f"This is a {origin_count}-origin January diagnostic and should not be interpreted as a stable seasonal estimate.",
            "Power is normalized; the farm equal-weight mean is a proxy, not capacity-weighted generation.",
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report

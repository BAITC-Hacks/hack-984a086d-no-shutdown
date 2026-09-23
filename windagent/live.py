"""Live forecast orchestration with explicit weather and physics provenance."""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import datetime, timedelta, timezone
from src import physics
from pathlib import Path
from typing import Callable

import pandas as pd

from .agent import ForecastError, _iso, validate_physics_predictions
from .model import predict_power
from .storage import ForecastStore


PROJECT_HOME = Path(__file__).resolve().parents[1]
_STATUS_LOCK = threading.Lock()
_STATUS = {"last_successful_issue_at": None, "last_check_at": None, "error": None}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ceil_hour(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    floored = value.replace(minute=0, second=0, microsecond=0)
    return floored + timedelta(hours=1)


def _timestamp(value: object, field: str) -> datetime:
    try:
        parsed = pd.Timestamp(value)
    except Exception as exc:
        raise ForecastError(f"Live weather has an invalid {field}") from exc
    if parsed.tzinfo is None:
        raise ForecastError(f"Live weather {field} must be timezone-aware")
    return parsed.tz_convert("UTC").to_pydatetime()


def _hash_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class LiveForecastAgent:
    """Fetch current weather and run the trusted turbine models for 24–48 hours."""

    def __init__(self, home: Path, *, weather_fetch: Callable | None = None,
                 predict: Callable | None = None, now: Callable[[], datetime] = _utcnow):
        self.home = Path(home).resolve()
        self.artifact_dir = self.home / "artifacts"
        self.cache_dir = self.home / "data" / "live_weather"
        self.config_path = self.home / "config" / "turbines.json"
        self.store = ForecastStore(self.home / "state" / "windagent.sqlite3")
        self._run_lock = threading.Lock()
        if weather_fetch is None:
            from .live_weather import fetch_live_weather
            weather_fetch = fetch_live_weather
        self.weather_fetch = weather_fetch
        self.predict = predict or predict_power
        self.now = now

    def _configuration(self) -> dict:
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ForecastError(f"Cannot read turbine configuration: {exc}") from exc
        turbines = config.get("turbines")
        capacity = config.get("capacity_mw")
        if not isinstance(turbines, dict) or isinstance(capacity, bool) or not isinstance(capacity, (int, float)) or not math.isfinite(capacity) or capacity <= 0:
            raise ForecastError("Turbine configuration must declare a positive capacity_mw and turbine coordinates")
        if set(turbines) != {"1", "2"}:
            raise ForecastError("Live forecasts require configured coordinates for turbines 1 and 2")
        for key, coords in turbines.items():
            if not isinstance(coords, dict) or not all(isinstance(coords.get(k), (int, float)) and math.isfinite(coords[k]) for k in ("latitude", "longitude")):
                raise ForecastError(f"Turbine {key} has invalid coordinates")
            if not -90 <= coords["latitude"] <= 90 or not -180 <= coords["longitude"] <= 180:
                raise ForecastError(f"Turbine {key} coordinates are outside valid ranges")
        return config

    def _model_metadata(self) -> tuple[dict, str, str, int]:
        metadata_path = self.artifact_dir / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ForecastError(f"Cannot read model metadata: {exc}") from exc
        trained_values = [t.get("training_last_hour_utc") for t in metadata.get("turbines", {}).values() if t.get("training_last_hour_utc")]
        if not trained_values:
            raise ForecastError("Model metadata has no training_last_hour_utc; model age cannot be assessed")
        trained_at = max((_timestamp(x, "training_last_hour_utc") for x in trained_values))
        cutoff_value = metadata.get("model_available_at") or metadata.get("availability_cutoff") or metadata.get("cutoff")
        if not cutoff_value:
            raise ForecastError("Model metadata does not declare model_available_at")
        cutoff = _timestamp(cutoff_value, "model_available_at")
        checked_at = self.now().astimezone(timezone.utc)
        if trained_at > checked_at or cutoff > checked_at:
            raise ForecastError("Model training or availability cutoff is later than the live forecast time")
        digest = hashlib.sha256()
        for path in sorted(p for p in self.artifact_dir.rglob("*") if p.is_file()):
            digest.update(path.relative_to(self.artifact_dir).as_posix().encode())
            digest.update(path.read_bytes())
        age_days = max(0, int((self.now().astimezone(timezone.utc) - trained_at).total_seconds() // 86400))
        return metadata, digest.hexdigest(), trained_at.isoformat(), age_days

    @staticmethod
    def _validate_weather(payload: dict, origin: datetime, hours: int, turbine_id: int) -> tuple[pd.DataFrame, dict, dict]:
        if not isinstance(payload, dict) or not isinstance(payload.get("frame"), pd.DataFrame):
            raise ForecastError(f"Live weather for turbine {turbine_id} did not return a frame")
        frame = payload["frame"].copy()
        missing = {"timestamp", "wind_speed", "temperature"} - set(frame.columns)
        if missing:
            raise ForecastError(f"Live weather for turbine {turbine_id} is missing fields: {', '.join(sorted(missing))}")
        ts = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        expected = pd.date_range(pd.Timestamp(origin), periods=hours, freq="h", tz="UTC")
        if ts.isna().any() or len(frame) != hours or not ts.reset_index(drop=True).equals(pd.Series(expected)):
            raise ForecastError(f"Live weather for turbine {turbine_id} must contain exactly {hours} consecutive UTC hours from forecast_start")
        frame["timestamp"] = ts
        for col in ("wind_speed", "temperature"):
            vals = pd.to_numeric(frame[col], errors="coerce")
            if vals.isna().any() or not vals.map(math.isfinite).all():
                raise ForecastError(f"Live weather for turbine {turbine_id} has missing or non-finite {col}")
            frame[col] = vals.astype(float)
        current = payload.get("current")
        provenance = payload.get("provenance")
        if not isinstance(current, dict) or not isinstance(provenance, dict):
            raise ForecastError(f"Live weather for turbine {turbine_id} lacks current or provenance metadata")
        current_time = _timestamp(current.get("valid_time"), "current valid_time")
        if abs((origin - current_time).total_seconds()) > 7200:
            raise ForecastError(f"Live weather current conditions for turbine {turbine_id} are stale or too far in the future")
        for field in ("wind_speed_10m", "wind_speed_100m", "temperature_2m"):
            try:
                value = float(current[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise ForecastError(f"Live weather current data is missing {field}") from exc
            if not math.isfinite(value):
                raise ForecastError(f"Live weather current {field} is non-finite")
        cache = provenance.get("cache", {})
        age = cache.get("age_seconds") if isinstance(cache, dict) else None
        if not isinstance(age, (int, float)) or age < 0 or age > 300:
            raise ForecastError(f"Live weather for turbine {turbine_id} is stale; refusing to forecast from cache age {age!r}s")
        if not provenance.get("source_hash") or not provenance.get("retrieved_at"):
            raise ForecastError(f"Live weather for turbine {turbine_id} lacks acquisition provenance")
        return frame.reset_index(drop=True), current | {"valid_time": current_time.isoformat()}, provenance

    def run(self, horizon: int = 48, refresh: bool = False) -> dict:
        """Serialize live acquisitions so poll and browser requests cannot race."""
        with self._run_lock:
            return self._run_unlocked(horizon, refresh)

    def _run_unlocked(self, horizon: int = 48, refresh: bool = False) -> dict:
        if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon not in (24, 48):
            raise ForecastError("Live horizon must be 24 or 48 hours")
        try:
            config = self._configuration()
            metadata, model_hash, trained_at, age_days = self._model_metadata()
            physics_path = Path(physics.__file__).resolve()
            if not physics_path.is_file():
                raise ForecastError("Physics sanity-check implementation is missing: src/physics.py")
            physics_hash = hashlib.sha256(physics_path.read_bytes()).hexdigest()
            pipeline_hash = _hash_json({name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                       for name in ("live.py", "model.py", "agent.py", "live_weather.py")})
        except Exception as exc:
            self._set_status(None, str(exc) or exc.__class__.__name__)
            raise
        config_hash = _hash_json(config)
        origin = _ceil_hour(self.now())
        attempts = 0
        while True:
            origin_key = _iso(origin)
            forecast_id = self.store.start(origin_key, horizon)
            try:
                self.store.event(forecast_id, "live_acquire", "started", {"forecast_start": origin_key, "horizon": horizon, "refresh": refresh})
                frames, currents, provenances = {}, {}, {}
                for turbine_id in (1, 2):
                    payload = self.weather_fetch(turbine_id, horizon, self.cache_dir, refresh=refresh, forecast_start=origin, now=self.now())
                    frame, current, provenance = self._validate_weather(payload, origin, horizon, turbine_id)
                    expected_coords = config["turbines"][str(turbine_id)]
                    requested_coords = provenance.get("requested_coordinates", {})
                    if not isinstance(requested_coords, dict) or any(abs(float(requested_coords.get(axis, math.inf)) - float(expected_coords[axis])) > 1e-6 for axis in ("latitude", "longitude")):
                        raise ForecastError(f"Weather provider request coordinates do not match configured turbine {turbine_id}")
                    frames[turbine_id], currents[turbine_id], provenances[turbine_id] = frame, current, provenance
                issued_at = self.now().astimezone(timezone.utc)
                next_hour = _ceil_hour(issued_at)
                if origin < next_hour and attempts < 1:
                    self.store.event(forecast_id, "live_acquire", "restarted", {"reason": "forecast_start passed before acquisition completed", "next_forecast_start": next_hour.isoformat()})
                    self.store.fail(forecast_id, "Forecast start passed during acquisition; reacquiring for the next full hour")
                    origin = next_hour
                    attempts += 1
                    continue
                if origin < next_hour:
                    raise ForecastError("Acquisition repeatedly crossed a UTC hour boundary; retry live forecast")
                age = max(0, int((issued_at - _timestamp(trained_at, "trained_at")).total_seconds() // 86400))
                input_components = {
                    str(t): {"rows": frames[t].assign(timestamp=frames[t]["timestamp"].map(lambda x: x.isoformat())).to_dict(orient="records"),
                             "source_hash": provenances[t]["source_hash"]}
                    for t in (1, 2)
                }
                input_hash = _hash_json(input_components)
                combined_model_hash = _hash_json({"model": model_hash, "config": config_hash, "physics": physics_hash,
                                                 "pipeline": pipeline_hash})
                self.store.event(forecast_id, "live_acquire_validate", "succeeded", {"input_hash": input_hash, "model_hash": model_hash, "config_hash": config_hash, "physics_hash": physics_hash, "provenance": provenances})
                cached = None if refresh else self.store.find_reusable(origin_key, horizon, input_hash, combined_model_hash)
                if cached:
                    result = cached["result"]
                    result["id"] = forecast_id
                    result["reused"] = True
                    result["reused_from"] = cached["id"]
                    result["checked_at"] = issued_at.isoformat()
                    for turbine in result["turbines"]:
                        tid = int(turbine["turbine_id"])
                        turbine["current"] = currents[tid]
                        turbine["provenance"] = provenances[tid]
                    checked_at = self.now().astimezone(timezone.utc)
                    next_hour = _ceil_hour(checked_at)
                    if origin < next_hour:
                        self.store.fail(forecast_id, "Forecast start passed during response assembly; retrying for next full hour")
                        if attempts < 1:
                            origin = next_hour
                            attempts += 1
                            continue
                        raise ForecastError("Live forecast repeatedly crossed a UTC hour boundary; retry")
                    result["checked_at"] = checked_at.isoformat()
                    self.store.finish(forecast_id, result, input_hash, combined_model_hash)
                    self.store.event(forecast_id, "live_reuse", "succeeded", {"source_forecast_id": cached["id"], "checked_at": result["checked_at"]})
                    self._set_status(result["issued_at"], None)
                    return result

                turbines = []
                for turbine_id in (1, 2):
                    prediction = self.predict(self.artifact_dir, turbine_id, frames[turbine_id]).copy()
                    required = {"timestamp", "power", "lower", "upper"}
                    if required - set(prediction.columns) or len(prediction) != horizon:
                        raise ForecastError(f"Model returned invalid output for turbine {turbine_id}")
                    prediction["timestamp"] = pd.to_datetime(prediction["timestamp"], utc=True, errors="coerce")
                    if prediction["timestamp"].isna().any() or not prediction["timestamp"].reset_index(drop=True).equals(frames[turbine_id]["timestamp"].reset_index(drop=True)):
                        raise ForecastError(f"Model timestamps do not match live weather for turbine {turbine_id}")
                    for col in ("power", "lower", "upper"):
                        prediction[col] = pd.to_numeric(prediction[col], errors="coerce")
                    if prediction[["power", "lower", "upper"]].isna().any().any() or not all(prediction[c].map(math.isfinite).all() for c in ("power", "lower", "upper")):
                        raise ForecastError(f"Model returned null or non-finite power for turbine {turbine_id}")
                    if ((prediction[["power", "lower", "upper"]] < 0) | (prediction[["power", "lower", "upper"]] > 1)).any().any() or (prediction["lower"] > prediction["power"]).any() or (prediction["power"] > prediction["upper"]).any():
                        raise ForecastError(f"Model returned invalid normalized bounds for turbine {turbine_id}")
                    prediction["wind_speed"] = frames[turbine_id]["wind_speed"].to_numpy()
                    raw = prediction[["power", "lower", "upper"]].copy()
                    # The teammate's implementation is the final authority for [0,1], cut-in, and cut-out checks.
                    for col in ("power", "lower", "upper"):
                        prediction = validate_physics_predictions(prediction, pred_col=col, wind_col="wind_speed")
                    prediction["lower"] = prediction[["lower", "power"]].min(axis=1)
                    prediction["upper"] = prediction[["upper", "power"]].max(axis=1)
                    corrections = {col: int((raw[col].to_numpy() != prediction[col].to_numpy()).sum()) for col in ("power", "lower", "upper")}
                    cap = float(config["capacity_mw"])
                    points = []
                    for i, row in prediction.iterrows():
                        normalized = float(row["power"])
                        points.append({
                            "timestamp": row["timestamp"].isoformat(),
                            "wind_speed": float(row["wind_speed"]),
                            "temperature": float(frames[turbine_id].iloc[i]["temperature"]),
                            "raw_normalized_power": float(raw.iloc[i]["power"]),
                            "raw_lower_normalized": float(raw.iloc[i]["lower"]),
                            "raw_upper_normalized": float(raw.iloc[i]["upper"]),
                            "raw_power_mw": float(raw.iloc[i]["power"] * cap),
                            "raw_lower_mw": float(raw.iloc[i]["lower"] * cap),
                            "raw_upper_mw": float(raw.iloc[i]["upper"] * cap),
                            "normalized_power": normalized,
                            "power_mw": normalized * cap,
                            "lower_mw": float(row["lower"] * cap),
                            "upper_mw": float(row["upper"] * cap),
                            "energy_mwh": normalized * cap,
                        })
                    turbines.append({"turbine_id": turbine_id, "coordinates": config["turbines"][str(turbine_id)],
                                     "current": currents[turbine_id], "provenance": provenances[turbine_id],
                                     "physics_corrections": sum(corrections.values()), "physics_correction_counts": corrections, "points": points})

                farm_points = []
                for i in range(horizon):
                    a, b = turbines[0]["points"][i], turbines[1]["points"][i]
                    farm_points.append({"timestamp": a["timestamp"], "power_mw": a["power_mw"] + b["power_mw"],
                                        "lower_mw": a["lower_mw"] + b["lower_mw"], "upper_mw": a["upper_mw"] + b["upper_mw"],
                                        "energy_mwh": a["energy_mwh"] + b["energy_mwh"]})
                issued_at = self.now().astimezone(timezone.utc)
                next_hour = _ceil_hour(issued_at)
                if origin < next_hour:
                    self.store.fail(forecast_id, "Forecast start passed during inference; retrying for next full hour")
                    if attempts < 1:
                        origin = next_hour
                        attempts += 1
                        continue
                    raise ForecastError("Live forecast repeatedly crossed a UTC hour boundary; retry")
                trained_time = _timestamp(trained_at, "trained_at")
                cutoff_time = _timestamp(metadata.get("model_available_at") or metadata.get("availability_cutoff") or metadata.get("cutoff"), "model_available_at")
                if trained_time > issued_at or cutoff_time > issued_at:
                    raise ForecastError("Model training or availability cutoff is later than the live forecast time")
                age = max(0, int((issued_at - trained_time).total_seconds() // 86400))
                result = {
                    "id": forecast_id, "status": "ok", "reused": False,
                    "issued_at": issued_at.isoformat(), "checked_at": issued_at.isoformat(),
                    "forecast_start": origin_key, "horizon_hours": horizon,
                    "model": {"name": metadata.get("model_version", "unknown"), "trained_at": trained_at,
                              "age_days": age, "age_warning": age > 180, "availability_cutoff": metadata.get("model_available_at")},
                    "capacity": {"per_turbine_mw": float(config["capacity_mw"]), "total_mw": float(config["capacity_mw"] * 2),
                                 "source": config.get("capacity_source", "configuration")},
                    "current_is_model_estimate": True,
                    "turbines": turbines,
                    "farm": {"points": farm_points, "peak_power_mw": max(p["power_mw"] for p in farm_points),
                             "total_energy_horizon_mwh": sum(p["energy_mwh"] for p in farm_points)},
                    "summary": {"energy_24h_mwh": sum(p["energy_mwh"] for p in farm_points[:24]),
                                "energy_horizon_mwh": sum(p["energy_mwh"] for p in farm_points),
                                "power_unit": "MW", "energy_unit": "MWh", "energy_interval_hours": 1},
                    "audit": {"input_hash": input_hash, "model_hash": model_hash, "configuration_hash": config_hash,
                              "physics_hash": physics_hash, "pipeline_hash": pipeline_hash,
                              "weather_source_hashes": {str(t): provenances[t]["source_hash"] for t in (1, 2)}},
                    "limitations": ["Current weather values are model estimates, not SCADA observations.",
                                    "Future power and energy are forecasts; intervals exclude weather forecast uncertainty."],
                }
                self.store.event(forecast_id, "live_infer_physics", "succeeded", {"rows_per_turbine": horizon, "physics_corrections": {str(t["turbine_id"]): t["physics_correction_counts"] for t in turbines}})
                self.store.finish(forecast_id, result, input_hash, combined_model_hash)
                self.store.event(forecast_id, "live_persist", "succeeded", {"forecast_id": forecast_id})
                self._set_status(result["issued_at"], None)
                return result
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                self.store.fail(forecast_id, message)
                self.store.event(forecast_id, "live_pipeline", "failed", {"error_type": exc.__class__.__name__, "message": message})
                self._set_status(None, message)
                if isinstance(exc, ForecastError):
                    raise
                raise ForecastError(f"Live forecast {forecast_id} failed: {message}") from exc

    @staticmethod
    def _set_status(issue: str | None, error: str | None) -> None:
        with _STATUS_LOCK:
            _STATUS["last_check_at"] = _utcnow().isoformat()
            if issue:
                _STATUS["last_successful_issue_at"] = issue
            _STATUS["error"] = error


def live_status(background_loop_enabled: bool = False) -> dict:
    with _STATUS_LOCK:
        current_status = "error" if _STATUS["error"] is not None else ("ok" if _STATUS["last_successful_issue_at"] else "pending")
        return {"status": current_status, **_STATUS,
                "background_loop_enabled": bool(background_loop_enabled)}

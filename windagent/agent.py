"""Deterministic orchestration policy for durable wind forecasts."""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from .storage import ForecastStore
from .weather import PROVENANCE_WARNING

PIPELINE_VERSION = "agent-v2-provenance"


class ForecastError(RuntimeError):
    """An actionable forecast pipeline error."""


def _parse_origin(value: str | datetime) -> datetime:
    try:
        stamp = pd.Timestamp(value)
    except Exception as exc:
        raise ForecastError(f"Invalid forecast origin: {value!r}") from exc
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ForecastError("Forecast origin must include a timezone offset")
    return stamp.tz_convert("UTC").to_pydatetime()


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")


def _frame_hash(frame: pd.DataFrame, provenance: dict) -> str:
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True).map(lambda v: v.isoformat())
    records = data.sort_values("timestamp").to_dict(orient="records")
    serialized = json.dumps({"rows": records, "provenance": provenance}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


class ForecastAgent:
    def __init__(self, home: Path, *, weather_fetch: Callable | None = None, predict: Callable | None = None,
                 strict_as_of: bool | None = None):
        self.home = Path(home).resolve()
        self.artifact_dir = self.home / "artifacts"
        self.cache_dir = self.home / "data" / "weather"
        self.store = ForecastStore(self.home / "state" / "windagent.sqlite3")
        self.strict_as_of = (os.environ.get("WINDAGENT_STRICT_AS_OF", "0") == "1"
                             if strict_as_of is None else strict_as_of)
        if weather_fetch is None:
            from .weather import fetch_weather
            weather_fetch = fetch_weather
        if predict is None:
            from .model import predict_power
            predict = predict_power
        self.weather_fetch = weather_fetch
        self.predict = predict

    def _metadata(self) -> dict:
        path = self.artifact_dir / "metadata.json"
        if not path.is_file():
            raise ForecastError(f"Trained model metadata is missing: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ForecastError(f"Cannot read model metadata at {path}: {exc}") from exc

    def _model_hash(self) -> str:
        if not self.artifact_dir.is_dir():
            raise ForecastError(f"Model artifact directory is missing: {self.artifact_dir}")
        digest = hashlib.sha256()
        files = sorted(p for p in self.artifact_dir.rglob("*") if p.is_file())
        if not files:
            raise ForecastError("No model artifacts are available")
        for path in files:
            digest.update(path.relative_to(self.artifact_dir).as_posix().encode())
            digest.update(path.read_bytes())
        # Do not reuse persisted v1 results with missing covariates/provenance.
        digest.update(PIPELINE_VERSION.encode())
        return digest.hexdigest()

    @staticmethod
    def _validate_weather(frame: pd.DataFrame, origin: datetime, horizon: int, turbine_id: int) -> tuple[pd.DataFrame, dict]:
        required = {"timestamp", "wind_speed", "temperature", "initialized_at", "available_at", "source", "source_hash"}
        missing = required - set(frame.columns)
        if missing:
            raise ForecastError(f"Weather for turbine {turbine_id} is missing fields: {', '.join(sorted(missing))}")
        out = frame.copy()
        for col in ("timestamp", "initialized_at", "available_at"):
            for value in out[col]:
                parsed = pd.Timestamp(value)
                if parsed.tzinfo is None:
                    raise ForecastError(f"Weather for turbine {turbine_id} has timezone-naive {col}; UTC-aware timestamps are required")
        ts = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        if ts.isna().any():
            raise ForecastError(f"Weather for turbine {turbine_id} has invalid timestamps")
        out["timestamp"] = ts
        expected = pd.date_range(pd.Timestamp(origin), periods=horizon, freq="h", tz="UTC")
        if len(out) != horizon or not out["timestamp"].sort_values().reset_index(drop=True).equals(pd.Series(expected)):
            raise ForecastError(f"Weather for turbine {turbine_id} must contain exactly {horizon} consecutive hours from origin")
        for col in ("wind_speed", "temperature"):
            vals = pd.to_numeric(out[col], errors="coerce")
            if vals.isna().any() or not vals.map(math.isfinite).all():
                raise ForecastError(f"Weather for turbine {turbine_id} contains missing or non-finite {col}")
            out[col] = vals.astype(float)
        if (out["wind_speed"] < 0).any():
            raise ForecastError(f"Weather for turbine {turbine_id} contains negative wind speed")
        for col in ("initialized_at", "available_at"):
            vals = pd.to_datetime(out[col], utc=True, errors="coerce")
            if vals.isna().any():
                raise ForecastError(f"Weather for turbine {turbine_id} has invalid {col} provenance")
            if col == "available_at" and (vals > pd.Timestamp(origin)).any():
                raise ForecastError(f"Weather for turbine {turbine_id} was not available at forecast origin")
            out[col] = vals
        if (out["initialized_at"] > out["available_at"]).any():
            raise ForecastError(f"Weather for turbine {turbine_id} has availability before model initialization")
        if any(out[col].isna().any() or out[col].astype(str).str.strip().eq("").any()
               for col in ("source", "source_hash")):
            raise ForecastError(f"Weather for turbine {turbine_id} lacks source provenance")
        if any(out[col].nunique() != 1 for col in ("initialized_at", "available_at", "source", "source_hash")):
            raise ForecastError(f"Weather for turbine {turbine_id} mixes multiple model runs")
        # Omitted metadata never becomes implicit proof of historical issuance.
        for key, default in (("availability_basis", "unverified"),
                             ("provenance_status", "unverified"), ("as_of_verified", False)):
            if key not in out:
                out[key] = default
        if not out["as_of_verified"].map(lambda value: isinstance(value, (bool, type(pd.Series([True]).iloc[0])))).all():
            raise ForecastError("as_of_verified must be a boolean, not a string or number")
        # A verified adapter must include a publication evidence reference. The
        # built-in Open-Meteo adapter intentionally cannot satisfy this contract.
        verified = out["as_of_verified"].eq(True)
        if verified.any() and ("publication_evidence" not in out or
                               out.loc[verified, "publication_evidence"].isna().any() or
                               out.loc[verified, "publication_evidence"].astype(str).str.strip().eq("").any()):
            raise ForecastError("Verified historical availability requires publication evidence")
        provenance = {k: sorted(map(str, out[k].unique())) for k in (
            "initialized_at", "available_at", "source", "source_hash", "availability_basis", "provenance_status")}
        provenance["as_of_verified"] = bool(verified.all())
        if "publication_evidence" in out:
            provenance["publication_evidence"] = sorted(map(str, out["publication_evidence"].unique()))
        return out.sort_values("timestamp").reset_index(drop=True), provenance

    def run(self, origin: str, horizon: int = 48, refresh: bool = False) -> dict:
        if not isinstance(horizon, int) or isinstance(horizon, bool) or not 24 <= horizon <= 48:
            raise ForecastError("Horizon must be an integer from 24 through 48 hours")
        stamp = _parse_origin(origin)
        if stamp.minute or stamp.second or stamp.microsecond:
            raise ForecastError("Forecast origin must be aligned to the start of an hour")
        origin_key = _iso(stamp)
        forecast_id = self.store.start(origin_key, horizon)
        try:
            self.store.event(forecast_id, "acquire", "started", {"origin": origin_key, "horizon": horizon, "refresh": refresh})
            metadata = self._metadata()
            cutoff_value = metadata.get("model_available_at") or metadata.get("availability_cutoff") or metadata.get("cutoff")
            if not cutoff_value:
                raise ForecastError("Model metadata does not declare model_available_at; refusing to forecast without an as-of guard")
            cutoff = _parse_origin(cutoff_value)
            if stamp < cutoff:
                raise ForecastError(f"Forecast origin {_iso(stamp)} precedes model availability cutoff {_iso(cutoff)}")
            for turbine_id, turbine_meta in metadata.get("turbines", {}).items():
                last_hour = turbine_meta.get("training_last_hour_utc")
                if last_hour:
                    training_complete = _parse_origin(last_hour) + timedelta(hours=1)
                    if training_complete > cutoff or training_complete > stamp:
                        raise ForecastError(f"Model training for turbine {turbine_id} includes an hour ending after its availability cutoff or forecast origin")
            model_hash = self._model_hash()
            frames, provenance, frame_hashes = {}, {}, {}
            for turbine_id in (1, 2):
                raw = self.weather_fetch(turbine_id, stamp, horizon, self.cache_dir, refresh=refresh)
                frame, prov = self._validate_weather(raw, stamp, horizon, turbine_id)
                if self.strict_as_of and not prov["as_of_verified"]:
                    raise ForecastError("Strict as-of mode refused unverified historical weather. " + PROVENANCE_WARNING)
                frames[turbine_id] = frame
                provenance[turbine_id] = prov
                frame_hashes[str(turbine_id)] = _frame_hash(frame, prov)
            input_hash = hashlib.sha256(json.dumps(frame_hashes, sort_keys=True).encode()).hexdigest()
            if model_hash != self._model_hash() or metadata != self._metadata():
                raise ForecastError("Model artifacts changed during acquisition; retry with a stable trained model")
            self.store.event(forecast_id, "acquire_validate", "succeeded", {"input_hash": input_hash, "provenance": provenance})
            cached = self.store.find_reusable(origin_key, horizon, input_hash, model_hash)
            if cached:
                result = cached["result"]
                result.update({"id": forecast_id, "status": "succeeded", "reused": True, "reused_from": cached["id"],
                               "generated_at": datetime.now(timezone.utc).isoformat()})
                self.store.finish(forecast_id, result, input_hash, model_hash,
                                  stage="reuse", detail={"source_forecast_id": cached["id"]})
                return result
            self.store.event(forecast_id, "infer", "started", {"model_hash": model_hash})
            turbines = {}
            for turbine_id in (1, 2):
                pred = self.predict(self.artifact_dir, turbine_id, frames[turbine_id])
                required = {"timestamp", "power", "lower", "upper"}
                if required - set(pred.columns) or len(pred) != horizon:
                    raise ForecastError(f"Model returned an invalid prediction table for turbine {turbine_id}")
                pred = pred.copy()
                pred["timestamp"] = pd.to_datetime(pred["timestamp"], utc=True, errors="coerce")
                if pred["timestamp"].isna().any() or not pred["timestamp"].reset_index(drop=True).equals(frames[turbine_id]["timestamp"].reset_index(drop=True)):
                    raise ForecastError(f"Model returned timestamps that do not match weather for turbine {turbine_id}")
                for col in ("power", "lower", "upper"):
                    pred[col] = pd.to_numeric(pred[col], errors="coerce")
                if pred[list(required - {"timestamp"})].isna().any().any() or not all(pred[c].map(math.isfinite).all() for c in ("power", "lower", "upper")):
                    raise ForecastError(f"Model returned null or non-finite values for turbine {turbine_id}")
                if ((pred[["power", "lower", "upper"]] < 0) | (pred[["power", "lower", "upper"]] > 1)).any().any() or (pred["lower"] > pred["power"]).any() or (pred["power"] > pred["upper"]).any():
                    raise ForecastError(f"Model returned invalid normalized power bounds for turbine {turbine_id}")
                rows = pred[["timestamp", "power", "lower", "upper"]].to_dict(orient="records")
                turbines[str(turbine_id)] = [{**row, "timestamp": row["timestamp"].isoformat()} for row in rows]
            farm = []
            for i in range(horizon):
                a, b = turbines["1"][i], turbines["2"][i]
                farm.append({"timestamp": a["timestamp"], "normalized_power_mean_proxy": (a["power"] + b["power"]) / 2})
            farm_values = [r["normalized_power_mean_proxy"] for r in farm]
            ramps = [abs(farm_values[i] - farm_values[i - 1]) for i in range(1, len(farm_values))]
            intervals = [((r["upper"] - r["lower"]) / 2) for turbine_rows in turbines.values() for r in turbine_rows]
            mean_power = sum(farm_values) / horizon
            peak_ramp = max(ramps, default=0.0)
            mean_half_width = sum(intervals) / len(intervals)
            result = {
                "id": forecast_id, "status": "succeeded", "reused": False,
                "origin": origin_key, "horizon": horizon,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "pipeline_version": PIPELINE_VERSION,
                "as_of_verified": all(prov["as_of_verified"] for prov in provenance.values()),
                "mode": "strict_historical" if self.strict_as_of else "historical_reconstruction",
                "weather": {str(tid): [
                    {"timestamp": row.timestamp.isoformat(), "wind_speed": float(row.wind_speed),
                     "temperature": float(row.temperature)}
                    for row in frame.itertuples()
                ] for tid, frame in frames.items()},
                "model": {"version": metadata.get("model_version"), "available_at": cutoff.isoformat(), "hash": model_hash},
                "turbines": turbines,
                "farm": {"label": "equal-weight normalized power mean proxy; capacity unavailable", "series": farm},
                "analysis": {"farm_mean_normalized_power_proxy": mean_power,
                             "farm_min_normalized_power_proxy": min(farm_values), "farm_max_normalized_power_proxy": max(farm_values),
                             "peak_hourly_ramp_proxy": peak_ramp, "mean_conditional_interval_half_width": mean_half_width,
                             "decision": "review_low_generation" if mean_power < 0.15 else ("review_rapid_ramp" if peak_ramp >= 0.25 else "normal_monitoring"),
                             "warnings": ["Wide conditional residual interval; weather uncertainty is not included."] if mean_half_width >= 0.25 else [],
                             "note": "Power is normalized model output; farm proxy is not kW or energy. Intervals describe conditional residual spread and omit weather forecast uncertainty."},
                "provenance": provenance,
                "warnings": [] if all(prov["as_of_verified"] for prov in provenance.values()) else [PROVENANCE_WARNING],
            }
            if model_hash != self._model_hash() or metadata != self._metadata():
                raise ForecastError("Model artifacts changed during inference; retry with a stable trained model")
            self.store.event(forecast_id, "infer_analyze", "succeeded", {"rows_per_turbine": horizon, "farm_proxy": True})
            self.store.finish(forecast_id, result, input_hash, model_hash)
            return result
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self.store.fail(forecast_id, message)
            self.store.event(forecast_id, "pipeline", "failed", {"error_type": exc.__class__.__name__, "message": message})
            if isinstance(exc, ForecastError):
                raise
            raise ForecastError(f"Forecast {forecast_id} failed: {message}") from exc

    def replay(self, start: str = "2026-02-01T00:00:00+05:00", days: int = 28, horizon: int = 48, *, end: str | None = None) -> dict:
        try:
            first = pd.Timestamp(start)
            _parse_origin(start)
        except (ValueError, TypeError, ForecastError) as exc:
            raise ForecastError("Replay start must be a valid timestamp with a timezone offset") from exc
        if end is not None:
            try:
                last = pd.Timestamp(end)
                _parse_origin(end)
            except (ValueError, TypeError, ForecastError) as exc:
                raise ForecastError("Replay end must be a valid timestamp with a timezone offset") from exc
            last = last.tz_convert(first.tz)
            if last < first:
                raise ForecastError("Replay end must be on or after start")
            days = (last.date() - first.date()).days + 1
        if not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > 31:
            raise ForecastError("Replay range must contain from 1 through 31 local days")
        outputs = []
        for i in range(days):
            origin = first + pd.Timedelta(days=i)
            try:
                outputs.append(self.run(origin.isoformat(), horizon=horizon))
            except ForecastError as exc:
                outputs.append({"origin": origin.isoformat(), "status": "failed", "error": str(exc)})
        return {"status": "completed_with_failures" if any(x["status"] == "failed" for x in outputs) else "succeeded",
                "origins": outputs, "actuals_available": False,
                "metrics": None, "metrics_note": "February ground truth is unavailable; replay metrics cannot be computed."}

    def watch_once(self, origin: str, horizon: int = 48) -> dict:
        """One polling iteration; acquisition hashes decide reuse versus recomputation."""
        return self.run(origin, horizon=horizon, refresh=True)

    def run_loop(self, stop_event: threading.Event, *, interval_seconds: float = 300,
                 origin_provider: Callable[[], str] | None = None) -> None:
        """Callable autonomous loop. A supplied origin provider controls deterministic scheduling."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        provider = origin_provider or (lambda: datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat())
        while not stop_event.is_set():
            try:
                self.watch_once(provider())
            except ForecastError:
                # The durable audit row contains the actionable failure. Continue on next tick.
                pass
            stop_event.wait(interval_seconds)

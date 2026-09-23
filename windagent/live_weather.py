"""Live Open-Meteo ECMWF forecast acquisition with a validated short-lived cache."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

API_URL = "https://api.open-meteo.com/v1/forecast"
MODEL = "ecmwf_ifs"
SOURCE = "Open-Meteo Forecast API"
TTL_SECONDS = 300
MAX_RETRIES = 3
TURBINES: dict[int, tuple[float, float]] = {
    1: (43.645150, 78.535604),
    2: (43.643198, 78.538828),
}


class LiveWeatherError(RuntimeError):
    """The live weather request or its provenance failed validation."""


class LiveWeatherUnavailableError(LiveWeatherError):
    """A fresh, complete live forecast could not be obtained."""


def _utc(value: Any, name: str) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"Invalid {name}: {value!r}") from exc
    if result.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return result.tz_convert("UTC")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _clock_now() -> pd.Timestamp:
    """Return actual retrieval/check time; isolated to keep cache tests deterministic."""
    return pd.Timestamp.now(tz="UTC")


def _request_json(params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    url = f"{API_URL}?{urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            request = Request(url, headers={"User-Agent": "windagent-live/1.0"})
            with urlopen(request, timeout=35) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("error"):
                reason = payload.get("reason", "invalid API response") if isinstance(payload, dict) else "invalid API response"
                raise LiveWeatherUnavailableError(str(reason))
            return payload, hashlib.sha256(raw).hexdigest()
        except HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code != 429:
                raise LiveWeatherUnavailableError(f"Open-Meteo returned HTTP {exc.code}: {exc.reason}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
        if attempt + 1 < MAX_RETRIES:
            time.sleep(0.25 * 2**attempt)
    raise LiveWeatherUnavailableError(f"Open-Meteo request failed after {MAX_RETRIES} attempts: {last_error}") from last_error


def _params(turbine_id: int, horizon: int, start: pd.Timestamp) -> dict[str, Any]:
    lat, lon = TURBINES[turbine_id]
    # Include the current hour plus the requested range in the API response.
    return {
        "latitude": lat,
        "longitude": lon,
        "models": MODEL,
        "current": "wind_speed_10m,wind_speed_100m,temperature_2m",
        "hourly": "wind_speed_100m,temperature_2m",
        "wind_speed_unit": "ms",
        "temperature_unit": "celsius",
        "timezone": "UTC",
        "timeformat": "iso8601",
        "forecast_days": 3,
    }


def _validate_metadata(payload: dict[str, Any]) -> None:
    if payload.get("timezone") not in {"UTC", "GMT"} or payload.get("utc_offset_seconds") != 0:
        raise LiveWeatherUnavailableError("response timezone is not UTC with zero offset")
    units = payload.get("hourly_units")
    if not isinstance(units, dict) or units.get("wind_speed_100m") != "m/s" or units.get("temperature_2m") != "°C":
        raise LiveWeatherUnavailableError("hourly response omitted required units or used unexpected units")
    current_units = payload.get("current_units")
    expected = {"wind_speed_10m": "m/s", "wind_speed_100m": "m/s", "temperature_2m": "°C"}
    if not isinstance(current_units, dict) or any(current_units.get(key) != unit for key, unit in expected.items()):
        raise LiveWeatherUnavailableError("current response omitted required units or used unexpected units")


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LiveWeatherUnavailableError(f"invalid {label}") from exc
    if not math.isfinite(number):
        raise LiveWeatherUnavailableError(f"non-finite {label}")
    return number


def _validate_payload(payload: dict[str, Any], *, turbine_id: int, horizon: int,
                      start: pd.Timestamp, now: pd.Timestamp | None = None) -> tuple[pd.DataFrame, dict[str, Any], dict[str, float]]:
    _validate_metadata(payload)
    hourly = payload.get("hourly")
    current = payload.get("current")
    if not isinstance(hourly, dict) or not isinstance(current, dict):
        raise LiveWeatherUnavailableError("response omitted current or hourly values")
    try:
        times = pd.to_datetime(hourly["time"], utc=True, errors="raise")
        winds, temps = hourly["wind_speed_100m"], hourly["temperature_2m"]
        if not (len(times) == len(winds) == len(temps)):
            raise LiveWeatherUnavailableError("hourly arrays have inconsistent lengths")
        rows = pd.DataFrame({"timestamp": times, "wind_speed": pd.to_numeric(winds, errors="coerce"),
                             "temperature": pd.to_numeric(temps, errors="coerce")})
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveWeatherUnavailableError(f"response omitted valid hourly fields: {exc}") from exc
    if rows.timestamp.duplicated().any():
        raise LiveWeatherUnavailableError("response contains duplicate hourly timestamps")
    expected = pd.date_range(start, periods=horizon, freq="h", tz="UTC")
    frame = rows.set_index("timestamp").reindex(expected)
    if frame[["wind_speed", "temperature"]].isna().any().any():
        raise LiveWeatherUnavailableError("response is missing requested forecast hours or values")
    if (frame["wind_speed"] < 0).any():
        raise LiveWeatherUnavailableError("forecast contains negative wind speed")
    if not all(math.isfinite(float(v)) for v in frame.to_numpy().ravel()):
        raise LiveWeatherUnavailableError("forecast contains non-finite values")
    frame = frame.reset_index(names="timestamp")

    try:
        valid_time = pd.Timestamp(current["time"])
        if pd.isna(valid_time):
            raise ValueError("current valid time is missing")
        if valid_time.tzinfo is None:
            valid_time = valid_time.tz_localize("UTC")
        else:
            valid_time = valid_time.tz_convert("UTC")
        current_values = {
            "wind_speed_10m": _finite_number(current["wind_speed_10m"], "current wind_speed_10m"),
            "wind_speed_100m": _finite_number(current["wind_speed_100m"], "current wind_speed_100m"),
            "temperature_2m": _finite_number(current["temperature_2m"], "current temperature_2m"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveWeatherUnavailableError(f"invalid current conditions: {exc}") from exc
    if any(current_values[k] < 0 for k in ("wind_speed_10m", "wind_speed_100m")):
        raise LiveWeatherUnavailableError("current response contains negative wind speed")
    reference_now = now if now is not None else pd.Timestamp.now(tz="UTC")
    if valid_time > reference_now + pd.Timedelta(hours=1) or valid_time < reference_now - pd.Timedelta(hours=1):
        raise LiveWeatherUnavailableError("current conditions are outside the valid one-hour window")
    current_result = {"valid_time": valid_time.isoformat(), **current_values}

    expected_coords = TURBINES[turbine_id]
    try:
        returned = {"latitude": _finite_number(payload["latitude"], "latitude"),
                    "longitude": _finite_number(payload["longitude"], "longitude")}
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveWeatherUnavailableError("response omitted returned coordinates") from exc
    if abs(returned["latitude"] - expected_coords[0]) > 0.1 or abs(returned["longitude"] - expected_coords[1]) > 0.1:
        raise LiveWeatherUnavailableError("returned coordinates do not match requested turbine")
    return frame, current_result, returned


def _cache_path(cache_dir: Path, turbine_id: int, start: pd.Timestamp, horizon: int) -> Path:
    tag = start.strftime("%Y%m%dT%H%M%SZ")
    return Path(cache_dir) / f"turbine_{turbine_id}" / f"{tag}_h{horizon}_live.json"


def _write_atomic(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_path = handle.name
            json.dump(record, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _load_cache(path: Path, *, turbine_id: int, horizon: int, start: pd.Timestamp,
                now: pd.Timestamp) -> tuple[dict[str, Any], pd.DataFrame, dict[str, Any], dict[str, float], float] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("turbine_id") != turbine_id or record.get("horizon") != horizon or record.get("forecast_start") != start.isoformat():
            return None
        integrity = record.get("cache_integrity_sha256")
        unsigned_record = dict(record)
        unsigned_record.pop("cache_integrity_sha256", None)
        if integrity != hashlib.sha256(_canonical_json(unsigned_record).encode("utf-8")).hexdigest():
            return None
        expected_coords = {"latitude": TURBINES[turbine_id][0], "longitude": TURBINES[turbine_id][1]}
        if record.get("requested_coordinates") != expected_coords or record.get("model") != MODEL or record.get("initialization_time") is not None:
            return None
        retrieved = _utc(record["retrieved_at"], "retrieved_at")
        age = (now - retrieved).total_seconds()
        if age < 0 or age > TTL_SECONDS:
            return None
        payload = record["response"]
        if not isinstance(payload, dict):
            return None
        digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        if digest != record.get("source_hash"):
            return None
        frame, current, returned = _validate_payload(payload, turbine_id=turbine_id, horizon=horizon, start=start, now=now)
        if returned != record.get("returned_coordinates"):
            return None
        return record, frame, current, returned, age
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, LiveWeatherError):
        return None


def fetch_live_weather(turbine_id: int, horizon: int, cache_dir: Path, refresh: bool = False,
                       *, forecast_start: Any = None, now: Any = None) -> dict[str, Any]:
    """Fetch a fresh Open-Meteo ECMWF forecast for the exact requested UTC hours."""
    if isinstance(turbine_id, bool) or turbine_id not in TURBINES:
        raise ValueError(f"Unknown turbine_id: {turbine_id!r}")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 24 <= horizon <= 48:
        raise ValueError("horizon must be an integer from 24 through 48")
    now_utc = _utc(now, "now") if now is not None else pd.Timestamp.now(tz="UTC")
    if forecast_start is None:
        start = now_utc.floor("h") + pd.Timedelta(hours=1)
    else:
        start = _utc(forecast_start, "forecast_start")
    if start.minute or start.second or start.microsecond or start.nanosecond:
        raise ValueError("forecast_start must align to a full UTC hour")
    if start < now_utc.floor("h") + pd.Timedelta(hours=1):
        raise ValueError("forecast_start must be the next full UTC hour or later")
    path = _cache_path(cache_dir, turbine_id, start, horizon)
    if not refresh:
        cached = _load_cache(path, turbine_id=turbine_id, horizon=horizon, start=start, now=now_utc)
        if cached is not None:
            record, frame, current, returned, age = cached
            return {"frame": frame, "current": current, "provenance": {
                "source": SOURCE, "requested_coordinates": {"latitude": TURBINES[turbine_id][0], "longitude": TURBINES[turbine_id][1]},
                "returned_coordinates": returned, "retrieved_at": record["retrieved_at"],
                "source_hash": record["source_hash"], "model": MODEL, "initialization_time": None,
                "wind_height_m": 100, "cache": {"status": "hit", "age_seconds": age, "ttl_seconds": TTL_SECONDS},
            }}

    params = _params(turbine_id, horizon, start)
    try:
        payload, raw_response_hash = _request_json(params)
        frame, current, returned = _validate_payload(payload, turbine_id=turbine_id, horizon=horizon, start=start, now=now_utc)
    except LiveWeatherError:
        raise
    except Exception as exc:
        raise LiveWeatherUnavailableError(f"live forecast acquisition failed: {exc}") from exc
    retrieved_at = _clock_now().isoformat()
    canonical = _canonical_json(payload)
    source_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    record = {"schema_version": 1, "turbine_id": turbine_id, "horizon": horizon,
              "forecast_start": start.isoformat(), "requested_coordinates": {
                  "latitude": TURBINES[turbine_id][0], "longitude": TURBINES[turbine_id][1]},
              "returned_coordinates": returned, "retrieved_at": retrieved_at,
              "raw_response_sha256": raw_response_hash, "source_hash": source_hash,
              "model": MODEL, "initialization_time": None, "response": payload}
    record["cache_integrity_sha256"] = hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()
    _write_atomic(path, record)
    return {"frame": frame, "current": current, "provenance": {
        "source": SOURCE, "requested_coordinates": record["requested_coordinates"],
        "returned_coordinates": returned, "retrieved_at": retrieved_at, "source_hash": source_hash,
        "model": MODEL, "initialization_time": None, "wind_height_m": 100,
        "cache": {"status": "refreshed" if refresh else "miss", "age_seconds": 0.0, "ttl_seconds": TTL_SECONDS},
    }}

"""Acquisition of immutable ECMWF IFS HRES individual forecast runs."""

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

API_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
MODEL = "ecmwf_ifs"  # Open-Meteo's ECMWF IFS HRES 9 km model identifier.
SOURCE = "Open-Meteo Single Runs API / ECMWF IFS HRES 9 km"
PUBLICATION_LAG = timedelta(hours=12)  # Conservative assumed lag; not measured.
RUN_CADENCE_HOURS = 6
MAX_RETRIES = 3
TURBINES: dict[int, tuple[float, float]] = {
    1: (43.645150, 78.535604),
    2: (43.643198, 78.538828),
}


class WeatherError(RuntimeError):
    """Weather input could not be acquired or failed provenance validation."""


class WeatherUnavailableError(WeatherError):
    """The requested individual forecast run is not available or incomplete."""


def _as_utc_origin(origin: str | pd.Timestamp | datetime) -> pd.Timestamp:
    try:
        stamp = pd.Timestamp(origin)
    except Exception as exc:
        raise ValueError(f"Invalid origin: {origin!r}") from exc
    if stamp.tzinfo is None:
        raise ValueError("origin must include a timezone")
    return stamp.tz_convert("UTC")


def _run_for_origin(origin: pd.Timestamp) -> pd.Timestamp:
    """Select the latest 00/06/12/18 UTC run available under the lag rule."""
    threshold = origin - PUBLICATION_LAG
    # floor to a six-hour cycle in UTC; ECMWF IFS HRES runs every six hours.
    hour = (threshold.hour // RUN_CADENCE_HOURS) * RUN_CADENCE_HOURS
    return threshold.normalize() + pd.Timedelta(hours=hour)


def _canonical_hash(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _request_params(turbine_id: int, origin: pd.Timestamp, horizon: int,
                    initialized_at: pd.Timestamp) -> dict[str, Any]:
    latitude, longitude = TURBINES[turbine_id]
    lead_end = int((origin + pd.Timedelta(hours=horizon - 1) - initialized_at).total_seconds() // 3600) + 1
    return {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "wind_speed_100m,temperature_2m",
        "wind_speed_unit": "ms",
        "temperature_unit": "celsius",
        "timezone": "UTC",
        "timeformat": "iso8601",
        "models": MODEL,
        "run": initialized_at.strftime("%Y-%m-%dT%H:%M"),
        "forecast_hours": lead_end,
    }


def _validate_response_metadata(payload: dict[str, Any]) -> None:
    if payload.get("timezone") not in {"UTC", "GMT"} or payload.get("utc_offset_seconds") != 0:
        raise WeatherUnavailableError("response timezone is not UTC with zero offset")
    units = payload.get("hourly_units")
    if not isinstance(units, dict):
        raise WeatherUnavailableError("response omitted hourly units")
    if units.get("wind_speed_100m") != "m/s":
        raise WeatherUnavailableError("100 m wind speed units are not m/s")
    if units.get("temperature_2m") != "°C":
        raise WeatherUnavailableError("2 m temperature units are not Celsius")


def _semantic_source_hash(payload: dict[str, Any], *, turbine_id: int,
                          origin: pd.Timestamp, horizon: int,
                          initialized_at: pd.Timestamp) -> str:
    """Hash the selected forecast content plus the immutable run identity."""
    _validate_response_metadata(payload)
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherUnavailableError("response omitted hourly weather data")
    try:
        times = pd.to_datetime(hourly["time"], utc=True, errors="raise")
        winds = hourly["wind_speed_100m"]
        temperatures = hourly["temperature_2m"]
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherUnavailableError(f"response omitted required hourly fields: {exc}") from exc
    if not (len(times) == len(winds) == len(temperatures)):
        raise WeatherUnavailableError("response hourly arrays have inconsistent lengths")
    rows = pd.DataFrame({"time": times,
                         "wind_speed_100m": pd.to_numeric(winds, errors="coerce"),
                         "temperature_2m": pd.to_numeric(temperatures, errors="coerce")})
    if rows["time"].duplicated().any():
        raise WeatherUnavailableError("response contains duplicate hourly timestamps")
    rows = rows.set_index("time")
    expected = pd.date_range(origin, periods=horizon, freq="h", tz="UTC")
    selected = rows.reindex(expected)
    if selected.isna().any().any():
        raise WeatherUnavailableError("response is missing requested forecast hours or values")
    latitude, longitude = TURBINES[turbine_id]
    forecast = [{
        "time": stamp.strftime("%Y-%m-%dT%H:%M"),
        "wind_speed_100m": float(row.wind_speed_100m),
        "temperature_2m": float(row.temperature_2m),
    } for stamp, row in selected.iterrows()]
    if not all(math.isfinite(item[key]) for item in forecast
               for key in ("wind_speed_100m", "temperature_2m")):
        raise WeatherUnavailableError("forecast contains non-finite weather values")
    return _canonical_hash({
        "source": SOURCE, "model": MODEL, "run": initialized_at.isoformat(),
        "turbine_id": turbine_id, "coordinates": [latitude, longitude],
        "forecast": forecast,
    })


def _request_json(params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    url = f"{API_URL}?{urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            request = Request(url, headers={"User-Agent": "windagent/1.0"})
            with urlopen(request, timeout=40) as response:
                raw = response.read()
                data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or data.get("error"):
                raise WeatherUnavailableError(str(data.get("reason", "invalid API response")))
            return data, hashlib.sha256(raw).hexdigest()
        except HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code != 429:
                raise WeatherUnavailableError(f"Open-Meteo returned HTTP {exc.code}: {exc.reason}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
        if attempt + 1 < MAX_RETRIES:
            time.sleep(0.25 * (2**attempt))
    raise WeatherUnavailableError(f"Open-Meteo request failed after {MAX_RETRIES} attempts: {last_error}") from last_error


def _cache_path(cache_dir: Path, turbine_id: int, origin: pd.Timestamp, horizon: int) -> Path:
    origin_tag = origin.strftime("%Y%m%dT%H%M%SZ")
    return Path(cache_dir) / f"turbine_{turbine_id}" / f"{origin_tag}_h{horizon}.json"


def _validate_and_frame(
    payload: dict[str, Any], *, origin: pd.Timestamp, horizon: int,
    initialized_at: pd.Timestamp, available_at: pd.Timestamp, source_hash: str,
) -> pd.DataFrame:
    _validate_response_metadata(payload)
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherUnavailableError("response omitted hourly weather data")
    try:
        times = pd.to_datetime(hourly["time"], utc=True, errors="raise")
        wind = pd.to_numeric(hourly["wind_speed_100m"], errors="coerce")
        temp = pd.to_numeric(hourly["temperature_2m"], errors="coerce")
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherUnavailableError(f"response omitted required hourly fields: {exc}") from exc
    if not (len(times) == len(wind) == len(temp)):
        raise WeatherUnavailableError("response hourly arrays have inconsistent lengths")
    raw = pd.DataFrame({"timestamp": times, "wind_speed": wind, "temperature": temp})
    if raw["timestamp"].duplicated().any():
        raise WeatherUnavailableError("response contains duplicate hourly timestamps")
    expected = pd.date_range(origin, periods=horizon, freq="h", tz="UTC")
    frame = raw.set_index("timestamp").reindex(expected)
    if frame[["wind_speed", "temperature"]].isna().any().any():
        missing = expected[frame[["wind_speed", "temperature"]].isna().any(axis=1)].astype(str).tolist()
        raise WeatherUnavailableError(f"forecast run is missing requested hours or values: {missing[:3]}")
    if not all(math.isfinite(float(v)) for v in frame[["wind_speed", "temperature"]].to_numpy().ravel()):
        raise WeatherUnavailableError("forecast contains non-finite weather values")
    if (frame["wind_speed"] < 0).any():
        raise WeatherUnavailableError("forecast contains negative wind speed")
    frame.index.name = "timestamp"
    frame = frame.reset_index()
    frame["initialized_at"] = initialized_at
    frame["available_at"] = available_at
    frame["source"] = SOURCE
    frame["source_hash"] = source_hash
    if available_at > origin:
        raise WeatherUnavailableError("selected run was not available by the requested origin")
    return frame


def fetch_weather(
    turbine_id: int,
    origin: str | pd.Timestamp | datetime,
    horizon: int,
    cache_dir: Path,
    refresh: bool = False,
) -> pd.DataFrame:
    """Return one exact, as-of-safe forecast horizon from one initialized run.

    ``origin`` must be timezone-aware. Returned timestamps and run provenance
    are UTC-aware. Cached JSON contains the exact API response and acquisition
    metadata; ``source_hash`` excludes fetch time and response-generation time.
    """
    if turbine_id not in TURBINES:
        raise ValueError(f"Unknown turbine_id {turbine_id}; expected one of {sorted(TURBINES)}")
    if not isinstance(horizon, int) or isinstance(horizon, bool) or not 1 <= horizon <= 48:
        raise ValueError("horizon must be an integer from 1 through 48 hours")
    origin_utc = _as_utc_origin(origin)
    if origin_utc.minute or origin_utc.second or origin_utc.microsecond or origin_utc.nanosecond:
        raise ValueError("origin must be aligned to the start of an hour")
    initialized_at = _run_for_origin(origin_utc)
    available_at = initialized_at + PUBLICATION_LAG
    if available_at > origin_utc:
        raise WeatherUnavailableError("no ECMWF IFS run satisfies the publication-lag rule")
    path = _cache_path(cache_dir, turbine_id, origin_utc, horizon)
    if path.exists() and not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            latitude, longitude = TURBINES[turbine_id]
            expected_params = _request_params(turbine_id, origin_utc, horizon, initialized_at)
            expected_identity = {
                "model": MODEL, "source": SOURCE, "turbine_id": turbine_id,
                "coordinates": {"latitude": latitude, "longitude": longitude},
                "requested_origin": origin_utc.isoformat(), "horizon": horizon,
                "initialized_at": initialized_at.isoformat(), "available_at": available_at.isoformat(),
                "publication_lag_hours": PUBLICATION_LAG.total_seconds() / 3600,
                "request_params": expected_params,
            }
            if any(cached.get(key) != value for key, value in expected_identity.items()):
                raise WeatherUnavailableError("cached run provenance does not match the requested run")
            computed_hash = _semantic_source_hash(
                cached["response"], turbine_id=turbine_id, origin=origin_utc,
                horizon=horizon, initialized_at=initialized_at,
            )
            if cached.get("source_hash") != computed_hash:
                raise WeatherUnavailableError("cached source hash does not match forecast content")
            return _validate_and_frame(
                cached["response"], origin=origin_utc, horizon=horizon,
                initialized_at=initialized_at, available_at=available_at,
                source_hash=computed_hash,
            )
        except (WeatherError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # A bad cache is discarded and fetched anew; it is never returned.
            pass

    latitude, longitude = TURBINES[turbine_id]
    params = _request_params(turbine_id, origin_utc, horizon, initialized_at)
    response, raw_response_hash = _request_json(params)
    source_hash = _semantic_source_hash(response, turbine_id=turbine_id, origin=origin_utc,
                                        horizon=horizon, initialized_at=initialized_at)
    frame = _validate_and_frame(response, origin=origin_utc, horizon=horizon,
                                initialized_at=initialized_at, available_at=available_at,
                                source_hash=source_hash)
    cache_record = {
        "model": MODEL, "source": SOURCE, "turbine_id": turbine_id,
        "coordinates": {"latitude": latitude, "longitude": longitude},
        "requested_origin": origin_utc.isoformat(), "horizon": horizon,
        "initialized_at": initialized_at.isoformat(), "available_at": available_at.isoformat(),
        "publication_lag_hours": PUBLICATION_LAG.total_seconds() / 3600,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "request_params": params, "raw_response_sha256": raw_response_hash,
        "source_hash": source_hash, "response": response,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as temp_file:
            temp_name = temp_file.name
            json.dump(cache_record, temp_file, sort_keys=True, separators=(",", ":"))
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return frame

import json

import pandas as pd
import pytest

from windagent import weather


def payload(run="2026-01-31T06:00", hours=72):
    times = pd.date_range(run, periods=hours, freq="h", tz="UTC")
    return {
        "latitude": 43.6,
        "longitude": 78.5,
        "generationtime_ms": 2.0,
        "timezone": "GMT",
        "utc_offset_seconds": 0,
        "hourly_units": {
            "time": "iso8601",
            "wind_speed_100m": "m/s",
            "temperature_2m": "°C",
        },
        "hourly": {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
            "wind_speed_100m": [float(i) / 10 for i in range(hours)],
            "temperature_2m": [-4.0 + float(i) / 20 for i in range(hours)],
        },
    }


def test_fetch_uses_latest_run_available_by_conservative_lag_and_returns_exact_horizon(tmp_path, monkeypatch):
    calls = []

    def fake_request(params):
        calls.append(params)
        return payload(), "ignored-raw-hash"

    monkeypatch.setattr(weather, "_request_json", fake_request)
    origin = "2026-02-01T00:00:00+05:00"  # 2026-01-31 19:00 UTC
    frame = weather.fetch_weather(1, origin, 24, tmp_path)

    assert len(frame) == 24
    assert frame.timestamp.iloc[0] == pd.Timestamp("2026-01-31T19:00:00Z")
    assert frame.timestamp.iloc[-1] == pd.Timestamp("2026-02-01T18:00:00Z")
    assert frame.initialized_at.nunique() == 1
    assert frame.initialized_at.iloc[0] == pd.Timestamp("2026-01-31T06:00:00Z")
    assert frame.available_at.iloc[0] == pd.Timestamp("2026-01-31T18:00:00Z")
    assert frame.available_at.iloc[0] <= pd.Timestamp("2026-01-31T19:00:00Z")
    assert frame.source_hash.nunique() == 1
    assert frame.source.iloc[0] == weather.SOURCE
    assert calls[0]["models"] == "ecmwf_ifs"
    assert calls[0]["run"] == "2026-01-31T06:00"
    assert calls[0]["wind_speed_unit"] == "ms"
    assert calls[0]["forecast_hours"] == 37


def test_cache_keeps_source_hash_stable_across_fetch_times_and_refreshes(tmp_path, monkeypatch):
    calls = []

    def fake_request(params):
        calls.append(params)
        body = payload()
        body["generationtime_ms"] = len(calls) * 7
        return body, f"raw-{len(calls)}"

    monkeypatch.setattr(weather, "_request_json", fake_request)
    origin = "2026-02-01T00:00:00+05:00"
    first = weather.fetch_weather(2, origin, 24, tmp_path)
    cached = weather.fetch_weather(2, origin, 24, tmp_path)
    refreshed = weather.fetch_weather(2, origin, 24, tmp_path, refresh=True)
    assert len(calls) == 2
    assert first.source_hash.iloc[0] == cached.source_hash.iloc[0] == refreshed.source_hash.iloc[0]
    record = json.loads(next(tmp_path.rglob("*.json")).read_text())
    assert record["fetched_at"]
    assert record["raw_response_sha256"] == "raw-2"
    assert record["response"]["hourly"]["time"]


@pytest.mark.parametrize("origin", ["2026-02-01T00:00:00", "not-a-time"])
def test_rejects_naive_or_invalid_origin(tmp_path, origin):
    with pytest.raises(ValueError):
        weather.fetch_weather(1, origin, 24, tmp_path)


@pytest.mark.parametrize("horizon", [0, 49, 1.5, True])
def test_rejects_invalid_horizon(tmp_path, horizon):
    with pytest.raises(ValueError):
        weather.fetch_weather(1, "2026-02-01T00:00:00Z", horizon, tmp_path)


def test_rejects_invalid_turbine_and_unaligned_origin(tmp_path):
    with pytest.raises(ValueError):
        weather.fetch_weather(9, "2026-02-01T00:00:00Z", 24, tmp_path)
    with pytest.raises(ValueError):
        weather.fetch_weather(1, "2026-02-01T00:30:00Z", 24, tmp_path)


def test_rejects_missing_hours_and_nonfinite_values():
    origin = pd.Timestamp("2026-01-31T19:00:00Z")
    run = pd.Timestamp("2026-01-31T06:00:00Z")
    bad = payload(hours=36)
    bad["hourly"]["time"].pop()
    with pytest.raises(weather.WeatherUnavailableError):
        weather._validate_and_frame(bad, origin=origin, horizon=24, initialized_at=run,
                                    available_at=run + pd.Timedelta(hours=12), source_hash="x")
    bad = payload(hours=36)
    bad["hourly"]["wind_speed_100m"][13] = float("nan")
    with pytest.raises(weather.WeatherUnavailableError):
        weather._validate_and_frame(bad, origin=origin, horizon=24, initialized_at=run,
                                    available_at=run + pd.Timedelta(hours=12), source_hash="x")


def test_rejects_publication_after_origin():
    origin = pd.Timestamp("2026-01-31T18:00:00Z")
    run = pd.Timestamp("2026-01-31T06:00:00Z")
    with pytest.raises(weather.WeatherUnavailableError):
        weather._validate_and_frame(payload(), origin=origin, horizon=24, initialized_at=run,
                                    available_at=run + pd.Timedelta(hours=13), source_hash="x")


@pytest.mark.parametrize("tamper", ["source_hash", "response", "run", "coordinates", "params"])
def test_corrupt_cache_is_never_trusted(tmp_path, monkeypatch, tamper):
    calls = []

    def fake_request(params):
        calls.append(params)
        return payload(), f"raw-{len(calls)}"

    monkeypatch.setattr(weather, "_request_json", fake_request)
    origin = "2026-02-01T00:00:00+05:00"
    first = weather.fetch_weather(1, origin, 24, tmp_path)
    cache_path = next(tmp_path.rglob("*.json"))
    record = json.loads(cache_path.read_text())
    if tamper == "source_hash":
        record["source_hash"] = "0" * 64
    elif tamper == "response":
        record["response"]["hourly"]["wind_speed_100m"][13] += 1
    elif tamper == "run":
        record["initialized_at"] = "2026-01-31T12:00:00+00:00"
    elif tamper == "coordinates":
        record["coordinates"]["latitude"] += 0.01
    else:
        record["request_params"]["run"] = "2026-01-31T12:00"
    cache_path.write_text(json.dumps(record))

    recovered = weather.fetch_weather(1, origin, 24, tmp_path)
    assert len(calls) == 2
    assert recovered.source_hash.iloc[0] == first.source_hash.iloc[0]
    assert recovered.initialized_at.iloc[0] == pd.Timestamp("2026-01-31T06:00:00Z")


@pytest.mark.parametrize("metadata_change", [
    {"timezone": "Europe/Berlin", "utc_offset_seconds": 3600},
    {"hourly_units": {"wind_speed_100m": "km/h", "temperature_2m": "°C"}},
    {"hourly_units": {"wind_speed_100m": "m/s", "temperature_2m": "°F"}},
])
def test_rejects_non_utc_or_wrong_weather_units(tmp_path, monkeypatch, metadata_change):
    bad = payload()
    bad.update(metadata_change)
    monkeypatch.setattr(weather, "_request_json", lambda params: (bad, "raw"))
    with pytest.raises(weather.WeatherUnavailableError):
        weather.fetch_weather(1, "2026-02-01T00:00:00+05:00", 24, tmp_path)

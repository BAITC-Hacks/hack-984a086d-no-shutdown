import json

import pandas as pd
import pytest

from windagent import live_weather as weather


def payload(start="2026-09-23T16:00", hours=72):
    times = pd.date_range(start, periods=hours, freq="h", tz="UTC")
    return {
        "latitude": weather.TURBINES[1][0],
        "longitude": weather.TURBINES[1][1],
        "timezone": "GMT",
        "utc_offset_seconds": 0,
        "current_units": {"time": "iso8601", "wind_speed_10m": "m/s", "wind_speed_100m": "m/s", "temperature_2m": "°C"},
        "current": {"time": "2026-09-23T15:00", "wind_speed_10m": 4.3, "wind_speed_100m": 6.1, "temperature_2m": 12.2},
        "hourly_units": {"time": "iso8601", "wind_speed_100m": "m/s", "temperature_2m": "°C"},
        "hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
                   "wind_speed_100m": [5.0 + i / 100 for i in range(hours)],
                   "temperature_2m": [10.0 - i / 20 for i in range(hours)]},
    }


def fixed_times():
    return pd.Timestamp("2026-09-23T15:22:00Z"), pd.Timestamp("2026-09-23T16:00:00Z")


def install_fake(monkeypatch, calls=None, response_factory=None):
    calls = calls if calls is not None else []

    def fake(params):
        calls.append(params)
        body = response_factory() if response_factory else payload()
        return body, "a" * 64

    monkeypatch.setattr(weather, "_request_json", fake)
    return calls


def test_fetch_provides_exact_utc_frame_current_and_provenance(tmp_path, monkeypatch):
    calls = install_fake(monkeypatch)
    now, start = fixed_times()
    result = weather.fetch_live_weather(1, 24, tmp_path, forecast_start=start, now=now)
    frame = result["frame"]
    assert list(frame.columns) == ["timestamp", "wind_speed", "temperature"]
    assert len(frame) == 24
    assert pd.api.types.is_datetime64_any_dtype(frame.timestamp.dtype)
    assert frame.timestamp.dt.tz is not None and str(frame.timestamp.dt.tz) == "UTC"
    assert frame.timestamp.iloc[0] == start
    assert frame.timestamp.iloc[-1] == start + pd.Timedelta(hours=23)
    assert result["current"] == {"valid_time": "2026-09-23T15:00:00+00:00", "wind_speed_10m": 4.3,
                                 "wind_speed_100m": 6.1, "temperature_2m": 12.2}
    provenance = result["provenance"]
    assert provenance["source"] == weather.SOURCE
    assert provenance["model"] == "ecmwf_ifs"
    assert provenance["initialization_time"] is None
    assert provenance["wind_height_m"] == 100
    assert provenance["cache"]["status"] == "miss"
    assert calls[0]["models"] == "ecmwf_ifs"
    assert calls[0]["timezone"] == "UTC"
    assert calls[0]["current"] == "wind_speed_10m,wind_speed_100m,temperature_2m"


def test_default_start_is_next_full_hour(tmp_path, monkeypatch):
    install_fake(monkeypatch)
    now, _ = fixed_times()
    result = weather.fetch_live_weather(1, 24, tmp_path, now=now)
    assert result["frame"].timestamp.iloc[0] == pd.Timestamp("2026-09-23T16:00:00Z")


def test_fresh_cache_hit_and_refresh(tmp_path, monkeypatch):
    calls = install_fake(monkeypatch)
    now, start = fixed_times()
    clock = [now]
    monkeypatch.setattr(weather, "_clock_now", lambda: clock[0])
    first = weather.fetch_live_weather(1, 24, tmp_path, forecast_start=start, now=now)
    hit = weather.fetch_live_weather(1, 24, tmp_path, forecast_start=start, now=now + pd.Timedelta(seconds=40))
    clock[0] = now + pd.Timedelta(seconds=50)
    refreshed = weather.fetch_live_weather(1, 24, tmp_path, refresh=True, forecast_start=start, now=now + pd.Timedelta(seconds=50))
    assert len(calls) == 2
    assert hit["provenance"]["cache"] == {"status": "hit", "age_seconds": 40.0, "ttl_seconds": 300}
    assert refreshed["provenance"]["cache"]["status"] == "refreshed"
    assert first["provenance"]["source_hash"] == hit["provenance"]["source_hash"]


def test_expired_or_corrupt_cache_requires_new_fetch(tmp_path, monkeypatch):
    calls = install_fake(monkeypatch)
    now, start = fixed_times()
    clock = [now]
    monkeypatch.setattr(weather, "_clock_now", lambda: clock[0])
    weather.fetch_live_weather(1, 24, tmp_path, forecast_start=start, now=now)
    path = next(tmp_path.rglob("*_live.json"))
    record = json.loads(path.read_text())
    record["response"]["hourly"]["wind_speed_100m"][2] = 999
    path.write_text(json.dumps(record))
    clock[0] = now + pd.Timedelta(seconds=30)
    weather.fetch_live_weather(1, 24, tmp_path, forecast_start=start, now=now + pd.Timedelta(seconds=30))
    assert len(calls) == 2
    weather.fetch_live_weather(1, 24, tmp_path, forecast_start=start, now=now + pd.Timedelta(seconds=331))
    assert len(calls) == 3


@pytest.mark.parametrize("bad", [
    "hourly_units", "current_units", "time_zone", "missing_hour", "array_lengths", "nan", "negative_wind", "missing_current", "wrong_coordinates", "stale_current", "missing_current_time",
])
def test_invalid_responses_are_rejected(tmp_path, monkeypatch, bad):
    body = payload()
    if bad == "hourly_units":
        body["hourly_units"]["wind_speed_100m"] = "km/h"
    elif bad == "current_units":
        body["current_units"]["temperature_2m"] = "°F"
    elif bad == "time_zone":
        body["utc_offset_seconds"] = 3600
    elif bad == "missing_hour":
        body["hourly"]["time"].remove("2026-09-23T19:00")
        body["hourly"]["wind_speed_100m"].pop(3)
        body["hourly"]["temperature_2m"].pop(3)
    elif bad == "array_lengths":
        body["hourly"]["temperature_2m"].pop()
    elif bad == "nan":
        body["hourly"]["wind_speed_100m"][4] = float("nan")
    elif bad == "negative_wind":
        body["current"]["wind_speed_10m"] = -1
    elif bad == "missing_current":
        del body["current"]["wind_speed_100m"]
    elif bad == "wrong_coordinates":
        body["latitude"] = 1
    elif bad == "stale_current":
        body["current"]["time"] = "2026-09-23T13:00"
    elif bad == "missing_current_time":
        body["current"]["time"] = None
    install_fake(monkeypatch, response_factory=lambda: body)
    now, start = fixed_times()
    with pytest.raises(weather.LiveWeatherUnavailableError):
        weather.fetch_live_weather(1, 24, tmp_path, forecast_start=start, now=now)


@pytest.mark.parametrize("kwargs", [
    {"turbine_id": 3, "horizon": 24}, {"turbine_id": 1, "horizon": 23},
    {"turbine_id": 1, "horizon": 49}, {"turbine_id": 1, "horizon": True},
])
def test_invalid_turbine_or_horizon_fails_before_request(tmp_path, kwargs):
    with pytest.raises(ValueError):
        weather.fetch_live_weather(**kwargs, cache_dir=tmp_path)


def test_rejects_naive_and_unaligned_or_past_start(tmp_path):
    now, start = fixed_times()
    for bad in ["2026-09-23T16:00", "2026-09-23T16:30:00Z", "2026-09-23T15:00:00Z"]:
        with pytest.raises(ValueError):
            weather.fetch_live_weather(1, 24, tmp_path, forecast_start=bad, now=now)


def test_network_failure_does_not_return_stale_cache(tmp_path, monkeypatch):
    install_fake(monkeypatch)
    now, start = fixed_times()
    weather.fetch_live_weather(1, 24, tmp_path, forecast_start=start, now=now)
    monkeypatch.setattr(weather, "_request_json", lambda params: (_ for _ in ()).throw(weather.LiveWeatherUnavailableError("offline")))
    with pytest.raises(weather.LiveWeatherUnavailableError, match="offline"):
        weather.fetch_live_weather(1, 24, tmp_path, refresh=True, forecast_start=start,
                                   now=now + pd.Timedelta(seconds=20))

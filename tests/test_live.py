import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from windagent.agent import ForecastError
from windagent import live as live_module
from windagent.live import LiveForecastAgent


NOW = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)


def prepare_home(home: Path):
    (home / "artifacts").mkdir(parents=True)
    (home / "config").mkdir(parents=True)
    (home / "src").mkdir(parents=True)
    (home / "artifacts" / "metadata.json").write_text(json.dumps({
        "model_version": "test-model", "model_available_at": "2026-01-31T19:00:00Z",
        "turbines": {"1": {"training_last_hour_utc": "2026-01-31T18:00:00Z"},
                     "2": {"training_last_hour_utc": "2026-01-31T18:00:00Z"}},
    }))
    (home / "artifacts" / "model.bin").write_bytes(b"model")
    (home / "src" / "physics.py").write_bytes((Path(__file__).parents[1] / "src" / "physics.py").read_bytes())
    (home / "config" / "turbines.json").write_text(json.dumps({
        "capacity_mw": 2.5, "capacity_source": "user supplied",
        "turbines": {"1": {"latitude": 43.64515, "longitude": 78.535604},
                     "2": {"latitude": 43.643198, "longitude": 78.538828}},
    }))


def weather_factory(wind=5.0, age=0):
    def fetch(turbine_id, horizon, cache_dir, refresh=False, *, forecast_start=None, now=None):
        start = pd.Timestamp(forecast_start)
        return {
            "frame": pd.DataFrame({
                "timestamp": pd.date_range(start, periods=horizon, freq="h", tz="UTC"),
                "wind_speed": [wind] * horizon, "temperature": [8.0] * horizon,
            }),
            "current": {"valid_time": (start - pd.Timedelta(hours=1)).isoformat(),
                        "wind_speed_10m": 4.0, "wind_speed_100m": wind, "temperature_2m": 8.0},
            "provenance": {"source": "fixture", "source_hash": f"source-{turbine_id}",
                           "requested_coordinates": {1: {"latitude": 43.64515, "longitude": 78.535604}, 2: {"latitude": 43.643198, "longitude": 78.538828}}[turbine_id],
                           "retrieved_at": NOW.isoformat(), "cache": {"age_seconds": age, "status": "fixture"}},
        }
    return fetch


def predict(artifact_dir, turbine_id, frame):
    return frame[["timestamp"]].assign(power=0.5, lower=0.2, upper=0.8)


def test_live_physics_thresholds_match_teammate_boundary_rules(tmp_path):
    prepare_home(tmp_path)
    winds = [2.49, 2.5, 25.0, 25.01] * 6
    def fetch(turbine_id, horizon, cache_dir, refresh=False, *, forecast_start=None, now=None):
        start = pd.Timestamp(forecast_start)
        speeds = (winds * ((horizon + len(winds) - 1) // len(winds)))[:horizon]
        return {"frame": pd.DataFrame({"timestamp": pd.date_range(start, periods=horizon, freq="h"),
                                        "wind_speed": speeds, "temperature": [8.0] * horizon}),
                "current": {"valid_time": (start - pd.Timedelta(hours=1)).isoformat(), "wind_speed_10m": 4,
                            "wind_speed_100m": 5, "temperature_2m": 8},
                "provenance": {"source_hash": f"hash-{turbine_id}", "retrieved_at": NOW.isoformat(),
                               "requested_coordinates": {1: {"latitude": 43.64515, "longitude": 78.535604}, 2: {"latitude": 43.643198, "longitude": 78.538828}}[turbine_id],
                               "cache": {"age_seconds": 0}}}
    agent = LiveForecastAgent(tmp_path, weather_fetch=fetch, predict=predict, now=lambda: NOW)
    result = agent.run(24)
    points = result["turbines"][0]["points"]
    assert [points[i]["normalized_power"] for i in range(4)] == [0.0, 0.5, 0.5, 0.0]
    assert points[3]["raw_normalized_power"] == 0.5
    assert result["turbines"][0]["physics_corrections"] == 36
    assert result["turbines"][0]["physics_correction_counts"] == {"power": 12, "lower": 12, "upper": 12}


def test_live_energy_uses_mw_times_one_hour_as_mwh(tmp_path):
    prepare_home(tmp_path)
    result = LiveForecastAgent(tmp_path, weather_fetch=weather_factory(), predict=predict, now=lambda: NOW).run(24)
    assert result["turbines"][0]["points"][0]["power_mw"] == 1.25
    assert result["turbines"][0]["points"][0]["energy_mwh"] == 1.25
    assert result["farm"]["points"][0]["power_mw"] == 2.5
    assert result["summary"]["energy_24h_mwh"] == 60.0
    assert result["capacity"] == {"per_turbine_mw": 2.5, "total_mw": 5.0, "source": "user supplied"}


def test_live_rejects_unknown_or_missing_capacity(tmp_path):
    prepare_home(tmp_path)
    config = json.loads((tmp_path / "config" / "turbines.json").read_text())
    del config["capacity_mw"]
    (tmp_path / "config" / "turbines.json").write_text(json.dumps(config))
    agent = LiveForecastAgent(tmp_path, weather_fetch=weather_factory(), predict=predict, now=lambda: NOW)
    with pytest.raises(ForecastError, match="positive capacity_mw"):
        agent.run(24)


def test_live_refuses_stale_provider_cache(tmp_path):
    prepare_home(tmp_path)
    agent = LiveForecastAgent(tmp_path, weather_fetch=weather_factory(age=301), predict=predict, now=lambda: NOW)
    with pytest.raises(ForecastError, match="stale"):
        agent.run(24)


def test_reuse_preserves_original_issue_and_refreshes_current_metadata(tmp_path):
    prepare_home(tmp_path)
    clock = [NOW]
    fetch_count = {"value": 0}

    def fetch(turbine_id, horizon, cache_dir, refresh=False, *, forecast_start=None, now=None):
        fetch_count["value"] += 1
        start = pd.Timestamp(forecast_start)
        return {
            "frame": pd.DataFrame({"timestamp": pd.date_range(start, periods=horizon, freq="h"),
                                   "wind_speed": [5.0] * horizon, "temperature": [8.0] * horizon}),
            "current": {"valid_time": (start - pd.Timedelta(minutes=15)).isoformat(),
                        "wind_speed_10m": 4.0 + fetch_count["value"], "wind_speed_100m": 5.0, "temperature_2m": 8.0},
            "provenance": {"source_hash": f"stable-{turbine_id}", "retrieved_at": clock[0].isoformat(),
                           "requested_coordinates": {1: {"latitude": 43.64515, "longitude": 78.535604}, 2: {"latitude": 43.643198, "longitude": 78.538828}}[turbine_id],
                           "cache": {"age_seconds": 0}},
        }

    agent = LiveForecastAgent(tmp_path, weather_fetch=fetch, predict=predict, now=lambda: clock[0])
    first = agent.run(24)
    clock[0] = NOW + pd.Timedelta(minutes=1).to_pytimedelta()
    second = agent.run(24)
    assert second["reused"] is True
    assert second["issued_at"] == first["issued_at"]
    assert second["checked_at"] == clock[0].isoformat()
    assert second["turbines"][0]["current"]["wind_speed_10m"] != first["turbines"][0]["current"]["wind_speed_10m"]
    assert second["turbines"][0]["provenance"]["retrieved_at"] == clock[0].isoformat()


def test_force_refresh_bypasses_durable_reuse(tmp_path):
    prepare_home(tmp_path)
    fetch_flags = []
    predict_calls = []

    def fetch(turbine_id, horizon, cache_dir, refresh=False, *, forecast_start=None, now=None):
        fetch_flags.append(refresh)
        return weather_factory()(turbine_id, horizon, cache_dir, refresh, forecast_start=forecast_start, now=now)

    def recording_predict(*args):
        predict_calls.append(args[1])
        return predict(*args)

    agent = LiveForecastAgent(tmp_path, weather_fetch=fetch, predict=recording_predict, now=lambda: NOW)
    first = agent.run(24)
    forced = agent.run(24, refresh=True)
    assert first["reused"] is False
    assert forced["reused"] is False
    assert forced["id"] != first["id"]
    assert fetch_flags == [False, False, True, True]
    assert predict_calls == [1, 2, 1, 2]


def test_exact_hour_clock_starts_forecast_on_strictly_next_hour(tmp_path):
    prepare_home(tmp_path)
    exact_hour = datetime(2026, 9, 23, 11, 0, tzinfo=timezone.utc)
    starts = []

    def fetch(turbine_id, horizon, cache_dir, refresh=False, *, forecast_start=None, now=None):
        starts.append(pd.Timestamp(forecast_start))
        return weather_factory()(turbine_id, horizon, cache_dir, refresh, forecast_start=forecast_start, now=now)

    result = LiveForecastAgent(tmp_path, weather_fetch=fetch, predict=predict, now=lambda: exact_hour).run(24)
    assert result["forecast_start"] == "2026-09-23T12:00:00+00:00"
    assert all(start == pd.Timestamp("2026-09-23T12:00:00Z") for start in starts)


def test_acquisition_crossing_hour_reacquires_new_start(tmp_path):
    prepare_home(tmp_path)
    clock = [NOW]
    calls = {"now": 0}
    starts = []

    def ticking_now():
        calls["now"] += 1
        # _model_metadata, origin, and both weather calls see 10:30; issue time
        # sees the boundary crossed while provider acquisition was underway.
        if calls["now"] == 5:
            clock[0] = datetime(2026, 9, 23, 11, 0, 1, tzinfo=timezone.utc)
        return clock[0]

    def fetch(turbine_id, horizon, cache_dir, refresh=False, *, forecast_start=None, now=None):
        starts.append(pd.Timestamp(forecast_start))
        return weather_factory()(turbine_id, horizon, cache_dir, refresh, forecast_start=forecast_start, now=now)

    result = LiveForecastAgent(tmp_path, weather_fetch=fetch, predict=predict, now=ticking_now).run(24)
    assert result["forecast_start"] == "2026-09-23T12:00:00+00:00"
    assert starts[:2] == [pd.Timestamp("2026-09-23T11:00:00Z")] * 2
    assert starts[2:] == [pd.Timestamp("2026-09-23T12:00:00Z")] * 2


def test_inference_crossing_hour_reacquires_new_start(tmp_path):
    prepare_home(tmp_path)
    clock = [NOW]
    starts = []

    def fetch(turbine_id, horizon, cache_dir, refresh=False, *, forecast_start=None, now=None):
        starts.append(pd.Timestamp(forecast_start))
        return weather_factory()(turbine_id, horizon, cache_dir, refresh, forecast_start=forecast_start, now=now)

    def slow_predict(*args):
        clock[0] = datetime(2026, 9, 23, 11, 0, 1, tzinfo=timezone.utc)
        return predict(*args)

    result = LiveForecastAgent(tmp_path, weather_fetch=fetch, predict=slow_predict, now=lambda: clock[0]).run(24)
    assert result["forecast_start"] == "2026-09-23T12:00:00+00:00"
    assert starts == [pd.Timestamp("2026-09-23T11:00:00Z")] * 2 + [pd.Timestamp("2026-09-23T12:00:00Z")] * 2


def test_model_cutoff_between_issue_and_start_rejects_before_prediction(tmp_path):
    prepare_home(tmp_path)
    metadata_path = tmp_path / "artifacts" / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["model_available_at"] = "2026-09-23T10:45:00Z"
    metadata_path.write_text(json.dumps(metadata))
    fetch_calls, predict_calls = [], []
    def record_fetch(*args, **kwargs):
        fetch_calls.append(args)
        return weather_factory()(*args, **kwargs)
    agent = LiveForecastAgent(tmp_path, weather_fetch=record_fetch,
                              predict=lambda *args: predict_calls.append(args) or predict(*args), now=lambda: NOW)
    with pytest.raises(ForecastError, match="availability cutoff"):
        agent.run(24)
    assert fetch_calls == []
    assert predict_calls == []


def test_provider_requested_coordinates_must_match_turbine_config(tmp_path):
    prepare_home(tmp_path)
    base_fetch = weather_factory()

    def wrong_location(turbine_id, *args, **kwargs):
        payload = base_fetch(turbine_id, *args, **kwargs)
        payload["provenance"]["requested_coordinates"] = {"latitude": 0.0, "longitude": 0.0}
        return payload

    calls = []
    agent = LiveForecastAgent(tmp_path, weather_fetch=wrong_location, predict=lambda *args: calls.append(args) or predict(*args), now=lambda: NOW)
    with pytest.raises(ForecastError, match="coordinates do not match"):
        agent.run(24)
    assert calls == []


def test_failed_preflight_is_visible_in_live_status(tmp_path, monkeypatch):
    prepare_home(tmp_path)
    monkeypatch.setattr(live_module, "_STATUS", {"last_successful_issue_at": None, "last_check_at": None, "error": None})
    monkeypatch.setattr(live_module, "_utcnow", lambda: NOW)
    metadata_path = tmp_path / "artifacts" / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["model_available_at"] = "2026-09-23T10:45:00Z"
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ForecastError):
        LiveForecastAgent(tmp_path, weather_fetch=weather_factory(), predict=predict, now=lambda: NOW).run(24)
    status = live_module.live_status()
    assert status["status"] == "error"
    assert "availability cutoff" in status["error"]
    assert status["last_check_at"] == NOW.isoformat()

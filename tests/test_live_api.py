import json
from datetime import datetime, timezone

import pandas as pd
from fastapi.testclient import TestClient
import pytest

from windagent import api
from windagent.live import LiveForecastAgent


@pytest.fixture(autouse=True)
def disable_background_poll(monkeypatch):
    monkeypatch.setattr(api, "_POLL_ENABLED", False)


class LiveStub:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def run(self, hours=48, refresh=False):
        self.calls.append((hours, refresh))
        if self.error:
            raise RuntimeError(self.error)
        return {"status": "ok", "horizon_hours": hours, "issued_at": "2026-09-23T10:30:00+00:00"}


def test_live_api_passes_horizon_and_refresh(monkeypatch):
    agent = LiveStub()
    monkeypatch.setattr(api, "get_live_agent", lambda: agent)
    with TestClient(api.app) as client:
        response = client.get("/api/live", params={"hours": 24, "refresh": "true"})
    assert response.status_code == 200
    assert response.json()["horizon_hours"] == 24
    assert agent.calls == [(24, True)]


def test_live_api_returns_service_error_without_fallback(monkeypatch):
    monkeypatch.setattr(api, "get_live_agent", lambda: LiveStub("provider unavailable"))
    with TestClient(api.app) as client:
        response = client.get("/api/live", params={"hours": 48})
    assert response.status_code == 503
    assert "provider unavailable" in response.json()["detail"]


def test_live_api_rejects_unsupported_horizon(monkeypatch):
    monkeypatch.setattr(api, "get_live_agent", lambda: LiveStub())
    with TestClient(api.app) as client:
        response = client.get("/api/live", params={"hours": 25})
    assert response.status_code == 422


def test_status_endpoint_and_explicit_static_files(monkeypatch):
    monkeypatch.setattr(api, "_POLL_ENABLED", False)
    with TestClient(api.app) as client:
        status = client.get("/api/live/status")
        assert status.status_code == 200
        assert status.json()["background_loop_enabled"] is False
        assert client.get("/").status_code == 200
        assert client.get("/app.js").status_code == 200
        assert client.get("/styles.css").status_code == 200


def test_live_forecast_csv_exports_mw_and_mwh(tmp_path, monkeypatch):
    now = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)
    artifacts = tmp_path / "artifacts"
    config_dir = tmp_path / "config"
    artifacts.mkdir()
    config_dir.mkdir()
    (artifacts / "model.dat").write_bytes(b"test model")
    (artifacts / "metadata.json").write_text(json.dumps({
        "model_version": "test", "model_available_at": "2026-01-31T19:00:00Z",
        "turbines": {"1": {"training_last_hour_utc": "2026-01-31T18:00:00Z"},
                     "2": {"training_last_hour_utc": "2026-01-31T18:00:00Z"}},
    }))
    (config_dir / "turbines.json").write_text(json.dumps({
        "capacity_mw": 2.5, "capacity_source": "user supplied",
        "turbines": {"1": {"latitude": 43.64515, "longitude": 78.535604},
                     "2": {"latitude": 43.643198, "longitude": 78.538828}},
    }))

    def weather(turbine_id, horizon, cache_dir, refresh=False, *, forecast_start=None, now=None):
        start = pd.Timestamp(forecast_start)
        coordinates = {1: {"latitude": 43.64515, "longitude": 78.535604}, 2: {"latitude": 43.643198, "longitude": 78.538828}}[turbine_id]
        return {"frame": pd.DataFrame({"timestamp": pd.date_range(start, periods=horizon, freq="h"),
                                        "wind_speed": [5.0] * horizon, "temperature": [8.0] * horizon}),
                "current": {"valid_time": (start - pd.Timedelta(minutes=15)).isoformat(), "wind_speed_10m": 4.0,
                            "wind_speed_100m": 5.0, "temperature_2m": 8.0},
                "provenance": {"source_hash": f"sha-{turbine_id}", "retrieved_at": now.isoformat(),
                               "requested_coordinates": coordinates, "cache": {"age_seconds": 0}}}

    def prediction(artifact_dir, turbine_id, frame):
        return frame[["timestamp"]].assign(power=0.5, lower=0.2, upper=0.8)

    agent = LiveForecastAgent(tmp_path, weather_fetch=weather, predict=prediction, now=lambda: now)
    monkeypatch.setattr(api, "get_live_agent", lambda: agent)
    # Both API agents normally share WINDAGENT_HOME; point the historical store
    # accessor at this fixture's same database for the export request.
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    with TestClient(api.app) as client:
        response = client.get("/api/live", params={"hours": 24})
        assert response.status_code == 200, response.text
        forecast_id = response.json()["id"]
        exported = client.get(f"/forecasts/{forecast_id}/csv")
    assert exported.status_code == 200, exported.text
    assert "power_mw" in exported.text and "energy_mwh" in exported.text
    assert len(exported.text.splitlines()) == 49

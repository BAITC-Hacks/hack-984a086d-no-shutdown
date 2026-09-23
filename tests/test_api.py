import json

import pandas as pd
from fastapi.testclient import TestClient

from windagent import api
from windagent.agent import ForecastAgent


def weather(turbine_id, origin, horizon, cache_dir, refresh=False):
    origin = pd.Timestamp(origin).tz_convert("UTC")
    return pd.DataFrame({
        "timestamp": pd.date_range(origin, periods=horizon, freq="h"),
        "wind_speed": [6.0] * horizon, "temperature": [7.0] * horizon,
        "initialized_at": [origin - pd.Timedelta(hours=18)] * horizon,
        "available_at": [origin - pd.Timedelta(hours=6)] * horizon,
        "source": ["test"] * horizon, "source_hash": [f"hash-{turbine_id}"] * horizon,
    })


def predict(artifact_dir, turbine_id, frame):
    return frame[["timestamp"]].assign(power=0.5, lower=0.3, upper=0.7)


def test_forecast_detail_list_csv_and_health(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "metadata.json").write_text(json.dumps({"model_version": "test", "model_available_at": "2026-02-01T00:00:00+05:00"}))
    (artifacts / "model.bin").write_bytes(b"x")
    agent = ForecastAgent(tmp_path, weather_fetch=weather, predict=predict)
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    client = TestClient(api.app)
    assert client.get("/health").json()["status"] == "ok"
    response = client.post("/forecasts", json={"origin": "2026-02-01T00:00:00+05:00", "horizon": 24})
    assert response.status_code == 200, response.text
    forecast_id = response.json()["id"]
    assert client.get(f"/forecasts/{forecast_id}").json()["result"]["status"] == "succeeded"
    assert len(client.get("/forecasts").json()) == 1
    exported = client.get(f"/forecasts/{forecast_id}/csv")
    assert exported.status_code == 200 and "farm_normalized_power_mean_proxy" in exported.text


def test_api_returns_helpful_cutoff_error(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "metadata.json").write_text(json.dumps({"model_available_at": "2026-02-01T00:00:00+05:00"}))
    agent = ForecastAgent(tmp_path, weather_fetch=weather, predict=predict)
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    response = TestClient(api.app).post("/forecasts", json={"origin": "2026-01-31T00:00:00+05:00", "horizon": 24})
    assert response.status_code == 422
    assert "precedes model availability cutoff" in response.json()["detail"]


def test_replay_api_passes_requested_horizon_by_keyword(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "metadata.json").write_text(json.dumps({"model_available_at": "2026-02-01T00:00:00+05:00"}))
    agent = ForecastAgent(tmp_path, weather_fetch=weather, predict=predict)
    observed = {}
    def replay(start, days=28, horizon=48, *, end=None):
        observed.update(start=start, days=days, horizon=horizon, end=end)
        return {"status": "succeeded", "origins": [], "metrics_note": "none"}
    agent.replay = replay
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    response = TestClient(api.app).post("/replay", json={
        "start": "2026-02-01T00:00:00+05:00", "end": "2026-02-02T00:00:00+05:00", "horizon": 24,
    })
    assert response.status_code == 200
    assert observed == {
        "start": "2026-02-01T00:00:00+05:00", "days": 28, "horizon": 24,
        "end": "2026-02-02T00:00:00+05:00",
    }


def test_frontend_compatibility_forecast_uses_real_agent_contract(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "metadata.json").write_text(json.dumps({"model_version": "test", "model_available_at": "2026-02-01T00:00:00+05:00"}))
    (artifacts / "model.bin").write_bytes(b"x")
    agent = ForecastAgent(tmp_path, weather_fetch=weather, predict=predict)
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    response = TestClient(api.app).get("/api/forecast", params={
        "turbine_id": "T1", "as_of_date": "2026-02-01", "horizon_hours": 24,
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["turbine_id"] == "T1"
    assert payload["date_timezone"] == "Asia/Almaty"
    assert payload["power_unit"] == "normalized" and payload["capacity_mw"] is None
    assert len(payload["forecast"]) == 24
    assert set(payload["forecast"][0]) == {"timestamp", "predicted_power", "lower", "upper", "wind_speed", "temperature"}
    assert payload["forecast"][0]["predicted_power"] == 0.5
    assert payload["provenance"]["source"] == ["test"]
    assert any("МВт" in warning and "недоступен" in warning for warning in payload["warnings"])
    assert payload["as_of_verified"] is False
    assert len(payload["warning_details"]) >= 3


def test_frontend_compatibility_rejects_bad_query_values(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "metadata.json").write_text(json.dumps({"model_available_at": "2026-02-01T00:00:00+05:00"}))
    agent = ForecastAgent(tmp_path, weather_fetch=weather, predict=predict)
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    client = TestClient(api.app)
    for params in (
        {"turbine_id": "T3", "as_of_date": "2026-02-01"},
        {"turbine_id": "T1", "as_of_date": "2026-02-30"},
        {"turbine_id": "T1", "as_of_date": "2026-02-01", "horizon_hours": 23},
    ):
        assert client.get("/api/forecast", params=params).status_code == 422


def test_frontend_adapter_uses_persisted_weather_without_second_fetch(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "metadata.json").write_text(json.dumps({"model_available_at": "2026-02-01T00:00:00+05:00"}))
    calls = {"count": 0}
    def changing_weather(turbine_id, origin, horizon, cache_dir, refresh=False):
        calls["count"] += 1
        frame = weather(turbine_id, origin, horizon, cache_dir, refresh)
        if calls["count"] == 3:
            frame["wind_speed"] = 7.0
            frame["source_hash"] = "changed-content"
        return frame
    agent = ForecastAgent(tmp_path, weather_fetch=changing_weather, predict=predict)
    monkeypatch.setattr(api, "get_agent", lambda: agent)
    response = TestClient(api.app).get("/api/forecast", params={
        "turbine_id": "T1", "as_of_date": "2026-02-01", "horizon_hours": 24,
    })
    assert response.status_code == 200, response.text
    assert calls["count"] == 2  # Once per turbine, no extra adapter acquisition.
    assert response.json()["forecast"][0]["wind_speed"] == 6.0

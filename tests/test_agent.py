import json
from pathlib import Path

import pandas as pd
import pytest

from windagent.agent import ForecastAgent, ForecastError


def fixture_weather(turbine_id, origin, horizon, cache_dir, refresh=False, changed=False):
    origin = pd.Timestamp(origin).tz_convert("UTC")
    return pd.DataFrame({
        "timestamp": pd.date_range(origin, periods=horizon, freq="h"),
        "wind_speed": [5.0 + int(changed)] * horizon,
        "temperature": [8.0] * horizon,
        "initialized_at": [origin - pd.Timedelta(hours=18)] * horizon,
        "available_at": [origin - pd.Timedelta(hours=6)] * horizon,
        "source": ["fixture"] * horizon,
        "source_hash": [f"fixture-{turbine_id}-{int(changed)}"] * horizon,
    })


def fixture_predict(artifact_dir, turbine_id, weather):
    return weather[["timestamp", "wind_speed", "temperature"]].assign(power=0.4, lower=0.2, upper=0.6)


def make_agent(tmp_path, weather=None):
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "metadata.json").write_text(json.dumps({
        "model_version": "test-model", "model_available_at": "2026-02-01T00:00:00+05:00"
    }), encoding="utf-8")
    (tmp_path / "artifacts" / "turbine_1.bin").write_bytes(b"model")
    return ForecastAgent(tmp_path, weather_fetch=weather or fixture_weather, predict=fixture_predict)


def test_run_persists_auditable_forecast_and_reuses_unchanged_inputs(tmp_path):
    agent = make_agent(tmp_path)
    first = agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    second = agent.watch_once("2026-02-01T00:00:00+05:00", horizon=24)
    assert first["status"] == "succeeded"
    assert first["farm"]["label"].startswith("equal-weight normalized")
    assert isinstance(first["turbines"]["1"][0]["timestamp"], str)
    assert second["reused"] is True
    assert first["as_of_verified"] is False
    assert len(first["weather"]["1"]) == 24
    assert first["weather"]["1"][0]["wind_speed"] == 5.0
    assert first["warnings"]
    record = agent.store.get(first["id"])
    assert any(event["stage"] == "persist" for event in record["audit"])


def test_rejects_origin_before_model_cutoff_and_audits_failure(tmp_path):
    agent = make_agent(tmp_path)
    with pytest.raises(ForecastError, match="precedes model availability cutoff"):
        agent.run("2026-01-31T23:00:00+05:00", horizon=24)
    record = agent.store.list(1)[0]
    assert record["status"] == "failed"
    assert "cutoff" in record["error"]


def test_weather_failure_is_not_silently_replaced(tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("provider offline")
    agent = make_agent(tmp_path, broken)
    with pytest.raises(ForecastError, match="provider offline"):
        agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    assert agent.store.list(1)[0]["status"] == "failed"


def test_rejects_naive_weather_provenance(tmp_path):
    def naive(turbine_id, origin, horizon, cache_dir, refresh=False):
        frame = fixture_weather(turbine_id, origin, horizon, cache_dir)
        frame["available_at"] = "2026-01-31 18:00:00"
        return frame
    agent = make_agent(tmp_path, naive)
    with pytest.raises(ForecastError, match="timezone-naive"):
        agent.run("2026-02-01T00:00:00+05:00", horizon=24)


def test_rejects_initialization_after_availability_and_audits_failure(tmp_path):
    def invalid(turbine_id, origin, horizon, cache_dir, refresh=False):
        frame = fixture_weather(turbine_id, origin, horizon, cache_dir)
        frame["initialized_at"] = frame["available_at"] + pd.Timedelta(hours=1)
        return frame
    agent = make_agent(tmp_path, invalid)
    with pytest.raises(ForecastError, match="availability before model initialization"):
        agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    record = agent.store.list(1)[0]
    assert record["status"] == "failed"
    assert agent.store.get(record["id"])["audit"][-1]["status"] == "failed"


@pytest.mark.parametrize("invalid_kind", ["timestamp", "bounds"])
def test_rejects_invalid_model_outputs(tmp_path, invalid_kind):
    def invalid_predict(artifact_dir, turbine_id, frame):
        out = fixture_predict(artifact_dir, turbine_id, frame)
        if invalid_kind == "timestamp":
            out.loc[0, "timestamp"] = out.loc[0, "timestamp"] + pd.Timedelta(hours=1)
        else:
            out["power"] = 1.2
        return out
    agent = make_agent(tmp_path)
    agent.predict = invalid_predict
    with pytest.raises(ForecastError, match="timestamps|normalized power bounds"):
        agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    assert agent.store.list(1)[0]["status"] == "failed"


def test_model_hash_change_recomputes_same_origin(tmp_path):
    calls = {"count": 0}
    def counted_predict(artifact_dir, turbine_id, frame):
        calls["count"] += 1
        return fixture_predict(artifact_dir, turbine_id, frame)
    agent = make_agent(tmp_path)
    agent.predict = counted_predict
    first = agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    assert calls["count"] == 2
    with (tmp_path / "artifacts" / "turbine_1.bin").open("ab") as artifact:
        artifact.write(b"changed")
    second = agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    assert calls["count"] == 4
    assert second["reused"] is False
    assert first["model"]["hash"] != second["model"]["hash"]


def test_strict_mode_rejects_unverified_weather(tmp_path):
    agent = make_agent(tmp_path)
    agent.strict_as_of = True
    with pytest.raises(ForecastError, match="Strict as-of mode"):
        agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    assert agent.store.list(1)[0]["status"] == "failed"


def test_training_labels_after_declared_cutoff_are_rejected(tmp_path):
    agent = make_agent(tmp_path)
    path = tmp_path / "artifacts" / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata["turbines"] = {"1": {"training_last_hour_utc": "2026-01-31T19:00:00Z"}}
    path.write_text(json.dumps(metadata))
    with pytest.raises(ForecastError, match="Model training"):
        agent.run("2026-02-01T00:00:00+05:00", horizon=24)


def test_changed_weather_recomputes_and_retains_exact_inputs(tmp_path):
    changed = [False]
    def weather(*args, **kwargs):
        return fixture_weather(*args, **kwargs, changed=changed[0])
    agent = make_agent(tmp_path, weather)
    first = agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    changed[0] = True
    second = agent.watch_once("2026-02-01T00:00:00+05:00", horizon=24)
    assert not second["reused"]
    assert first["weather"]["1"][0]["wind_speed"] == 5.0
    assert second["weather"]["1"][0]["wind_speed"] == 6.0


def test_model_change_during_prediction_never_persists_success(tmp_path):
    agent = make_agent(tmp_path)
    def changing_predict(artifact_dir, turbine_id, frame):
        (artifact_dir / "turbine_1.bin").write_bytes(b"changed-mid-run")
        return fixture_predict(artifact_dir, turbine_id, frame)
    agent.predict = changing_predict
    with pytest.raises(ForecastError, match="artifacts changed"):
        agent.run("2026-02-01T00:00:00+05:00", horizon=24)
    assert agent.store.list(1)[0]["status"] == "failed"

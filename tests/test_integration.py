"""Supervisor checks using delivered models and actual archived weather, offline."""
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from windagent.agent import ForecastAgent, ForecastError
from windagent.api import app, get_agent
from windagent.weather import fetch_weather

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def real_agent(tmp_path, monkeypatch):
    shutil.copytree(ROOT / 'artifacts', tmp_path / 'artifacts')
    shutil.copytree(ROOT / 'data/weather', tmp_path / 'data/weather')
    def no_network(*args, **kwargs):
        raise AssertionError('Delivered replay cache should make this test fully offline')
    monkeypatch.setattr('windagent.weather.urlopen', no_network)
    return ForecastAgent(tmp_path)


def test_actual_models_and_archive_end_to_end(real_agent):
    result = real_agent.run('2026-02-01T00:00:00+05:00')
    assert result['status'] == 'succeeded'
    assert result['reused'] is False
    for key in ('1', '2'):
        rows = result['turbines'][key]
        assert len(rows) == 48
        assert pd.Timestamp(rows[0]['timestamp']) == pd.Timestamp(result['origin'])
        assert pd.Timestamp(rows[-1]['timestamp']) == pd.Timestamp(result['origin']) + pd.Timedelta(hours=47)
        for row in rows:
            assert 0 <= row['lower'] <= row['power'] <= row['upper'] <= 1
        prov = result['provenance'][int(key)]
        assert pd.Timestamp(prov['initialized_at'][0]) <= pd.Timestamp(prov['available_at'][0]) <= pd.Timestamp(result['origin'])
    again = real_agent.run('2026-01-31T19:00:00Z')
    assert again['reused'] is True
    assert again['turbines'] == result['turbines']
    assert any(event['stage'] == 'reuse' for event in real_agent.store.get(again['id'])['audit'])


def test_actual_model_rejects_pretraining_origin_before_network(real_agent):
    with pytest.raises(ForecastError, match='precedes model'):
        real_agent.run('2026-01-31T00:00:00+05:00')
    assert real_agent.store.list(1)[0]['status'] == 'failed'


def test_real_http_forecast_detail_and_csv(real_agent):
    app.dependency_overrides.clear()
    import windagent.api as api
    original = api.get_agent
    api.get_agent = lambda: real_agent
    try:
        with TestClient(app) as client:
            response = client.post('/forecasts', json={'origin': '2026-02-01T00:00:00+05:00', 'horizon': 48})
            assert response.status_code == 200, response.text
            forecast_id = response.json()['id']
            detail = client.get(f'/forecasts/{forecast_id}').json()
            assert detail['status'] == 'succeeded' and len(detail['audit']) >= 4
            csv = client.get(f'/forecasts/{forecast_id}/csv')
            assert csv.status_code == 200
            assert len(csv.text.strip().splitlines()) == 97
    finally:
        api.get_agent = original


def test_all_february_archives_are_asof_safe(monkeypatch):
    monkeypatch.setattr('windagent.weather.urlopen', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Unexpected network')))
    for day in range(1, 29):
        origin = pd.Timestamp(f'2026-02-{day:02d}T00:00:00+05:00')
        for turbine in (1, 2):
            frame = fetch_weather(turbine, origin, 48, ROOT / 'data/weather')
            assert len(frame) == 48
            assert np.isfinite(frame[['wind_speed', 'temperature']]).all().all()
            assert (frame['available_at'] <= origin).all()


@pytest.mark.parametrize('start,end', [('bad-date', None), ('2026-02-01T00:00:00+05:00', 'bad-date')])
def test_replay_rejects_malformed_dates(real_agent, start, end):
    with pytest.raises(ForecastError, match='valid timestamp'):
        real_agent.replay(start=start, end=end)

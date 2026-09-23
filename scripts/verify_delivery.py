"""Independent saved-result audit and live HTTP smoke test. Run from project root."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def verify() -> dict:
    metadata = json.loads((ROOT / 'artifacts/metadata.json').read_text(encoding='utf-8'))
    january = json.loads((ROOT / 'reports/january_backtest.json').read_text(encoding='utf-8'))
    rows = pd.read_csv(ROOT / 'reports/january_backtest_predictions.csv')
    rows['timestamp'] = pd.to_datetime(rows['timestamp'], utc=True)
    rows['origin'] = pd.to_datetime(rows['origin'], utc=True)
    report = {'source_hashes_match': True, 'january_rows': len(rows), 'january_metrics_recomputed': {}}
    for turbine in (1, 2):
        source = ROOT / f'data/raw/turbine_{turbine}.csv'
        assert hashlib.sha256(source.read_bytes()).hexdigest() == metadata['input_hashes'][str(turbine)]
        raw = pd.read_csv(source)
        # Independent reconstruction from supplied columns, rather than loader reuse.
        raw['timestamp'] = pd.to_datetime(raw.iloc[:, 1]).dt.tz_localize('Asia/Almaty', ambiguous='NaT', nonexistent='NaT').dt.tz_convert('UTC')
        power_column = raw.columns[3]
        truth = raw.dropna(subset=['timestamp']).set_index('timestamp')[power_column].resample('h').agg(['mean', 'count'])
        truth = truth[truth['count'] >= 4]['mean']
        subset = rows[rows.turbine_id == turbine]
        assert np.allclose(truth.reindex(subset.timestamp).to_numpy(), subset.power_actual.to_numpy())
        for bucket, part in subset.groupby('lead_bucket'):
            mae = float(np.abs(part.power_actual - part.power_predicted).mean())
            baseline = float(np.abs(part.power_actual - part.persistence_predicted).mean())
            recorded = january['metrics_by_turbine_and_lead'][str(turbine)][bucket]
            assert abs(mae - recorded['mae']) < 1e-10
            assert abs(baseline - recorded['persistence_baseline']['mae']) < 1e-10
            report['january_metrics_recomputed'][f'{turbine}:{bucket}'] = {'mae': mae, 'persistence_mae': baseline}
    assert (pd.to_datetime(rows.initialized_at, utc=True) + pd.Timedelta(hours=12) <= rows.origin).all()
    assert (rows.origin >= pd.Timestamp(january['model_available_at'])).all()
    february = pd.read_csv(ROOT / 'reports/february_forecasts.csv')
    assert len(february) == 28 * 48 * 2
    assert not february.duplicated(['origin', 'turbine_id', 'timestamp']).any()
    assert february.groupby(['origin', 'turbine_id']).size().eq(48).all()
    assert ((february.lower >= 0) & (february.lower <= february.power) & (february.power <= february.upper) & (february.upper <= 1)).all()
    assert (pd.to_datetime(february.timestamp, utc=True) - pd.to_datetime(february.origin, utc=True)).dt.total_seconds().div(3600).eq(february.lead_hour).all()
    report['february_origins'] = int(february.origin.nunique())
    report['february_rows'] = len(february)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    environment = dict(os.environ, WINDAGENT_HOME=str(ROOT))
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'windagent.api:app', '--host', '127.0.0.1', '--port', str(port)],
                               cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    try:
        for attempt in range(50):
            try:
                with urllib.request.urlopen(base + '/health', timeout=2) as response:
                    health = json.load(response)
                break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError('Server exited before readiness')
                time.sleep(0.1)
        else:
            raise RuntimeError('Server did not become ready')
        assert health['status'] == 'ok'
        request = urllib.request.Request(base + '/forecasts', data=json.dumps({'origin':'2026-02-01T00:00:00+05:00','horizon':48}).encode(), headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            forecast = json.load(response)
        assert forecast['status'] == 'succeeded'
        assert len(forecast['turbines']['1']) == 48
        with urllib.request.urlopen(base + f'/forecasts/{forecast["id"]}/csv', timeout=5) as response:
            assert len(response.read().decode().strip().splitlines()) == 97
        with urllib.request.urlopen(base + '/api/forecast?turbine_id=T1&as_of_date=2026-02-01&horizon_hours=48', timeout=30) as response:
            adapted = json.load(response)
        assert len(adapted['forecast']) == 48 and adapted['power_unit'] == 'normalized' and adapted['capacity_mw'] is None
        assert np.allclose([r['predicted_power'] for r in adapted['forecast']], [r['power'] for r in forecast['turbines']['1']])
        report['live_http_smoke'] = 'passed: health, forecast, CSV, dashboard compatibility endpoint over localhost TCP'
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    report['february_accuracy'] = 'unavailable: supplied actual observations stop on January 31'
    report['status'] = 'passed'
    (ROOT / 'reports/supervisor_verification.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    print(json.dumps(verify(), indent=2))

"""Independent saved-output audit; no sockets/network by default.

python scripts/verify_delivery.py
python scripts/verify_delivery.py --live-http  # optional localhost smoke test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def read_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding='utf-8'))


def independent_hourly(source: Path, *, timezone_name: str, semantics: str, min_samples: int) -> pd.Series:
    """Rebuild truth directly from CSV, without calling the production loader."""
    raw = pd.read_csv(source)
    time = pd.to_datetime(raw['Статистическое время'], errors='coerce')
    local = time.dt.tz_localize(timezone_name, ambiguous='NaT', nonexistent='NaT')
    columns = ['Средняя скорость ветра(m/s)', 'Нормализованная активная мощность', 'Средняя температура окружающей среды(°C)']
    numbers = raw[columns].apply(pd.to_numeric, errors='coerce')
    valid = (local.notna() & local.dt.minute.mod(10).eq(0) & local.dt.second.eq(0)
             & local.dt.microsecond.eq(0) & np.isfinite(numbers).all(axis=1)
             & numbers[columns[0]].between(0,100) & numbers[columns[1]].between(0,1)
             & numbers[columns[2]].between(-80,70))
    frame = pd.DataFrame({'timestamp': local.loc[valid].dt.tz_convert('UTC'), 'power': numbers.loc[valid,columns[1]]})
    if semantics == 'end':
        frame['timestamp'] -= pd.Timedelta(minutes=10)
    assert semantics in ('start','end')
    assert not frame.groupby('timestamp').power.nunique().gt(1).any(), 'Conflicting duplicate observations'
    frame = frame.drop_duplicates('timestamp').sort_values('timestamp').set_index('timestamp')
    grouped = frame.power.resample('h').agg(['mean','count'])
    return grouped.loc[grouped['count'] >= min_samples,'mean']


def metric_pair(part: pd.DataFrame, prediction: str) -> dict:
    error = part[prediction].to_numpy() - part.power_actual.to_numpy()
    return {'mae': float(np.abs(error).mean()), 'rmse': float(np.sqrt(np.square(error).mean())), 'bias': float(error.mean())}


def live_http_smoke() -> dict:
    """Explicit opt-in only; uses the portable stdlib server, not uvicorn."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    environment = dict(os.environ, WINDAGENT_HOME=str(ROOT), WINDAGENT_CHAT_PROVIDER='local')
    process = subprocess.Popen([sys.executable,'-m','windagent','serve','--host','127.0.0.1','--port',str(port)],
                               cwd=ROOT,env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    def get(path):
        with urllib.request.urlopen(base+path,timeout=30) as response:
            return response.read()
    try:
        for _ in range(100):
            try:
                health=json.loads(get('/health'))
                break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError('Portable server exited before readiness')
                time.sleep(.1)
        else:
            raise RuntimeError('Portable server did not become ready')
        assert health['status']=='ok'
        forecast=json.loads(get('/api/forecast?turbine_id=T1&as_of_date=2026-02-01&horizon_hours=48'))
        assert len(forecast['forecast'])==48 and forecast['as_of_verified'] is False
        assert forecast['power_unit']=='normalized' and forecast['capacity_mw'] is None
        assert len(get(f'/forecasts/{forecast["forecast_id"]}/csv').strip().splitlines())==97
        return {'status':'passed','server':'python -m windagent serve','transport':'localhost TCP'}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)


def verify(*, live_http: bool = False) -> dict:
    metadata = read_json('artifacts/metadata.json')
    january_meta = read_json('evaluation/january/artifacts/metadata.json')
    january = read_json('reports/january_backtest.json')
    february_json = read_json('reports/february_replay.json')
    profile = read_json('reports/dataset_profile.json')
    rows = pd.read_csv(ROOT/'reports/january_backtest_predictions.csv')
    conditional = pd.read_csv(ROOT/'reports/conditional_holdout_predictions.csv')
    for column in ('timestamp','origin','persistence_stale_timestamp'):
        rows[column]=pd.to_datetime(rows[column],utc=True)
    conditional['timestamp']=pd.to_datetime(conditional.timestamp,utc=True)
    config=metadata['training_config']
    assert metadata['model_version']=='conditional-power-v2'
    assert config['min_samples']==6 and profile['minimum_samples_per_hour']==6
    assert config['timestamp_semantics']==profile['timestamp_semantics_assumption']
    assert metadata['timezone']==profile['timezone_assumption']
    assert january_meta['training_config']['min_samples']==6
    assert january_meta['training_config']['timestamp_semantics']==config['timestamp_semantics']
    report={'verified_at':datetime.now(timezone.utc).isoformat(), 'model_version':metadata['model_version'],
            'source_hashes_match':True, 'artifact_hashes_match':True, 'hourly_minimum_samples':6,
            'timestamp_semantics_assumption':config['timestamp_semantics'], 'timezone_assumption':metadata['timezone'],
            'january_rows':len(rows), 'january_metrics_recomputed':{}, 'conditional_holdout_metrics_recomputed':{}}
    assert january['as_of_verified'] is False and january['provenance_status']=='unverified_hindcast'
    assert january['origins_completed']==7 and not january['failures']
    for turbine in (1,2):
        key=str(turbine)
        source=ROOT/f'data/raw/turbine_{turbine}.csv'
        assert hashlib.sha256(source.read_bytes()).hexdigest()==metadata['input_hashes'][key]==january_meta['input_hashes'][key]
        for owner, base in ((metadata,ROOT/'artifacts'),(january_meta,ROOT/'evaluation/january/artifacts')):
            info=owner['turbines'][key]
            artifact=base/info['artifact']
            assert artifact.resolve().parent==base.resolve()
            assert hashlib.sha256(artifact.read_bytes()).hexdigest()==info['artifact_sha256']
            assert pd.Timestamp(info['training_last_hour_utc'])+pd.Timedelta(hours=1)<=pd.Timestamp(owner['model_available_at'])
        truth=independent_hourly(source,timezone_name=metadata['timezone'],semantics=config['timestamp_semantics'],min_samples=config['min_samples'])
        assert len(truth)==profile['turbines'][key]['retained_hourly_rows']
        subset=rows[rows.turbine_id==turbine]
        assert np.allclose(truth.reindex(subset.timestamp).to_numpy(),subset.power_actual.to_numpy())
        expected_persistence=truth.reindex(subset.persistence_stale_timestamp).to_numpy()
        assert np.allclose(expected_persistence,subset.persistence_predicted.to_numpy())
        assert (subset.persistence_stale_timestamp+pd.Timedelta(hours=1)<=subset.origin).all()
        for bucket,part in subset.groupby('lead_bucket'):
            calculated=metric_pair(part,'power_predicted')
            persistence=metric_pair(part,'persistence_predicted')
            recorded=january['metrics_by_turbine_and_lead'][key][bucket]
            for metric in ('mae','rmse','bias'):
                assert abs(calculated[metric]-recorded[metric])<1e-10
                assert abs(persistence[metric]-recorded['persistence_baseline'][metric])<1e-10
            report['january_metrics_recomputed'][f'{turbine}:{bucket}']={**calculated,'persistence_mae':persistence['mae']}
        holdout=conditional[conditional.turbine_id==turbine]
        info=metadata['turbines'][key]
        selected=info['selected_candidate']
        assert set(holdout.selected_candidate)=={selected}
        assert np.allclose(truth.reindex(holdout.timestamp).to_numpy(),holdout.actual.to_numpy())
        errors=holdout[selected]-holdout.actual
        mae=float(errors.abs().mean());rmse=float(np.sqrt(np.square(errors).mean()))
        assert abs(mae-info['holdout']['mae'])<1e-10 and abs(rmse-info['holdout']['rmse'])<1e-10
        assert len(holdout)==info['holdout']['n']==744
        assert all(pd.Timestamp(fold['fit']['last_hour_utc'])<pd.Timestamp(fold['validation']['first_hour_utc'])
                   and pd.Timestamp(fold['validation']['last_hour_utc'])<holdout.timestamp.min()
                   for fold in info['rolling_validation_folds'])
        candidates=info['candidate_validation_metrics']
        expected=min(candidates,key=lambda name:candidates[name]['mean_fold_mae'])
        assert expected==selected
        report['conditional_holdout_metrics_recomputed'][key]={'n':len(holdout),'mae':mae,'rmse':rmse,'selected_candidate':selected,
            'note':'Observed-weather conditional fit; not operational day-ahead accuracy.'}
    assert len(rows)==7*48*2 and not rows.duplicated(['origin','turbine_id','timestamp']).any()
    assert (pd.to_datetime(rows.initialized_at,utc=True)+pd.Timedelta(hours=12)<=rows.origin).all()
    assert (rows.origin>=pd.Timestamp(january['model_available_at'])).all()
    february=pd.read_csv(ROOT/'reports/february_forecasts.csv')
    assert len(february)==28*48*2
    assert not february.duplicated(['origin','turbine_id','timestamp']).any()
    assert february.groupby(['origin','turbine_id']).size().eq(48).all()
    assert ((february.lower>=0)&(february.lower<=february.power)&(february.power<=february.upper)&(february.upper<=1)).all()
    assert (pd.to_datetime(february.timestamp,utc=True)-pd.to_datetime(february.origin,utc=True)).dt.total_seconds().div(3600).eq(february.lead_hour).all()
    assert february_json['status']=='succeeded' and len(february_json['origins'])==28
    assert february_json['actuals_available'] is False and february_json['metrics'] is None
    for forecast in february_json['origins']:
        assert forecast['status']=='succeeded' and forecast['as_of_verified'] is False and forecast['warnings']
        assert forecast['model']['version']==metadata['model_version']
        assert pd.Timestamp(forecast['origin'])>=pd.Timestamp(metadata['model_available_at'])
        for key in ('1','2'):
            predictions=forecast['turbines'][key]
            inputs=forecast['weather'][key]
            assert len(predictions)==len(inputs)==48
            assert [r['timestamp'] for r in inputs]==[r['timestamp'] for r in predictions]
            csv_rows=february[(february.origin==forecast['origin'])&(february.turbine_id==int(key))].sort_values('lead_hour')
            assert np.allclose(csv_rows.power,[r['power'] for r in predictions])
            assert np.allclose(csv_rows.lower,[r['lower'] for r in predictions])
            assert np.allclose(csv_rows.upper,[r['upper'] for r in predictions])
            provenance=forecast['provenance'][key]
            assert provenance['as_of_verified'] is False
            assert provenance['provenance_status']==['unverified_hindcast']
    report.update(january_origins=7,february_origins=int(february.origin.nunique()),february_rows=len(february),
                  conditional_holdout_rows=len(conditional),as_of_verified=False,
                  weather_provenance='unverified_hindcast; 12-hour availability is assumed, not documented publication',
                  february_accuracy='unavailable: supplied actual observations stop on January 31',
                  live_http_smoke=live_http_smoke() if live_http else {'status':'not_run','reason':'Default offline audit; --live-http is explicit opt-in. Current sandbox denies socket bind.'},
                  fastapi_pytest_suite={'status':'not_run','reason':'Optional FastAPI/pytest dependencies unavailable in this environment'},
                  independent_service_checks=read_json('reports/service_verification.json'),
                  independent_weather_checks=read_json('reports/weather_provenance_audit.json'),
                  status='passed_saved_output_audit')
    assert report['independent_service_checks']['passed']
    assert report['independent_weather_checks']['as_of_verified'] is False
    (ROOT/'reports/supervisor_verification.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-http',action='store_true',help='Explicitly launch localhost portable server and run HTTP smoke test')
    args=parser.parse_args()
    print(json.dumps(verify(live_http=args.live_http),indent=2,ensure_ascii=False))

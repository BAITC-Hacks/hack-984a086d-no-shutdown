"""Dependency-light, offline integration assertions for weather/agent policy."""
import json, tempfile, sys
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from windagent import weather
from windagent.agent import ForecastAgent, ForecastError

def forbidden_network(*a,**k):
    raise AssertionError('Unexpected network in offline verification')
weather.urlopen=forbidden_network
checks=[]
def check(name, fn):
    fn();checks.append(name);print('PASS:',name,flush=True)
def expect_error(call, text):
    try: call()
    except Exception as exc:
        assert text in str(exc), str(exc)
    else: raise AssertionError('Expected error: '+text)

def archive_checks():
    for month, days in [(1,range(1,8)),(2,range(1,29))]:
        for day in days:
            origin=f'2026-{month:02}-{day:02}T00:00:00+05:00'
            for tid in (1,2):
                full=weather.fetch_weather(tid,origin,48,ROOT/'data/weather')
                short=weather.fetch_weather(tid,origin,24,ROOT/'data/weather')
                assert len(full)==48 and len(short)==24
                assert not full.as_of_verified.any()
                assert set(full.provenance_status)=={'unverified_hindcast'}
                assert (full.available_at<=pd.Timestamp(origin)).all()
                pd.testing.assert_frame_equal(short[['timestamp','wind_speed','temperature']],full.iloc[:24][['timestamp','wind_speed','temperature']].reset_index(drop=True))
check('70 archived horizons + 70 offline 24-hour subsets; explicit unverified provenance',archive_checks)
check('strict fetch refuses unsupported historical availability',lambda:expect_error(lambda:weather.fetch_weather(1,'2026-02-01T00:00:00+05:00',48,ROOT/'data/weather',strict_as_of=True),'publication is unverified'))

with tempfile.TemporaryDirectory() as temp:
    home=Path(temp);art=home/'artifacts';art.mkdir()
    metadata={'model_version':'fixture','model_available_at':'2026-01-31T19:00:00Z','turbines':{'1':{'training_last_hour_utc':'2026-01-31T18:00:00Z'}}}
    (art/'metadata.json').write_text(json.dumps(metadata));(art/'model.bin').write_bytes(b'fixture')
    revision=[0];predict_count=[0]
    def fetch(tid, origin, horizon, cache_dir, refresh=False):
        frame=weather.fetch_weather(tid,origin,horizon,ROOT/'data/weather')
        frame['wind_speed']+=revision[0]
        frame['source_hash']=frame['source_hash'].map(lambda v:v+str(revision[0]))
        return frame
    def predict(artifact_dir,tid,frame):
        predict_count[0]+=1
        return frame.assign(power=.4,lower=.2,upper=.6)
    agent=ForecastAgent(home,weather_fetch=fetch,predict=predict)
    origin='2026-02-01T00:00:00+05:00'
    first=agent.run(origin,24)
    def persisted():
        assert not first['as_of_verified'] and first['warnings']
        assert len(first['weather']['1'])==24
        assert first['weather']['1'][0]['timestamp']==first['turbines']['1'][0]['timestamp']
        assert agent.store.get(first['id'])['audit'][-1]['stage']=='persist'
    check('durable exact weather, warnings and atomic audit',persisted)
    second=agent.watch_once(origin,24)
    check('unchanged watch input reuses predictions',lambda:(None if second['reused'] and predict_count[0]==2 else (_ for _ in ()).throw(AssertionError())))
    revision[0]=1;third=agent.watch_once(origin,24)
    def changed():
        assert not third['reused'] and predict_count[0]==4
        assert third['weather']['1'][0]['wind_speed']==first['weather']['1'][0]['wind_speed']+1
        assert agent.store.get(first['id'])['result']['weather']==first['weather']
    check('changed weather recalculates, earlier stored inputs remain intact',changed)
    agent.strict_as_of=True
    check('strict agent rejects unverified weather and logs failure',lambda:expect_error(lambda:agent.run(origin,24),'Strict as-of mode'))
    assert agent.store.list(1)[0]['status']=='failed'
    agent.strict_as_of=False
    metadata['turbines']['1']['training_last_hour_utc']='2026-01-31T19:00:00Z';(art/'metadata.json').write_text(json.dumps(metadata))
    check('training interval crossing cutoff is rejected',lambda:expect_error(lambda:agent.run(origin,24),'Model training'))
    metadata['turbines']['1']['training_last_hour_utc']='2026-01-31T18:00:00Z';(art/'metadata.json').write_text(json.dumps(metadata))
    (art/'model.bin').write_bytes(b'changed')
    def mutating(artifact_dir,tid,frame):
        (artifact_dir/'model.bin').write_bytes(b'mutated midrun')
        return predict(artifact_dir,tid,frame)
    agent.predict=mutating
    check('model modification during inference prevents success',lambda:expect_error(lambda:agent.run(origin,24),'artifacts changed'))
    assert agent.store.list(1)[0]['status']=='failed'

report={'verification':'offline standalone Python assertions; pytest suite not invoked by this script', 'checks_passed':checks,'archive_horizons_validated':70,'subset_horizons_validated':70,'network_requested':False,'as_of_verified':False,'limitation':'Cache integrity does not prove contemporaneous publication; provider labels IFS history hindcasts.'}
(ROOT/'reports/weather_provenance_audit.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))

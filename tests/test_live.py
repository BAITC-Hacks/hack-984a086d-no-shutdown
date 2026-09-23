"""Offline unittest coverage (also collectable by pytest)."""
import copy
import json
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
import pandas as pd
from windagent.agent import ForecastError
from windagent.live import LiveForecastAgent
from windagent.physics import calculate_physics_power_baseline

NOW=datetime(2026,9,23,10,30,tzinfo=timezone.utc)
ROOT=Path(__file__).resolve().parents[1]


class LiveAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);(self.home/'artifacts').mkdir();(self.home/'config').mkdir()
        self.config=json.loads((ROOT/'config/turbines.json').read_text())
        # Keep the safety-default fixture unverified even when project config
        # opts into an explicitly labeled capacity/normalization assumption.
        self.config.update(capacity_verified=False,capacity_evidence=None,
                           power_normalization='unknown',power_normalization_evidence=None)
        self.config['physics_scenario']['enabled']=False
        self.save_config()
        self.meta={'model_version':'test','model_available_at':'2026-01-31T19:00:00Z','turbines':{
            str(t):{'training_last_hour_utc':'2026-01-31T18:00:00Z','training_weather_ranges':{'wind_speed':[1,20],'temperature':[-20,40]}} for t in (1,2)}}
        self.save_meta();(self.home/'artifacts/model.bin').write_bytes(b'fixture')
        self.clock=NOW;self.speed=1.0;self.calls=[];self.predictions=0
        self.agent=LiveForecastAgent(self.home,weather_fetch=self.weather,predict=self.predict,now=lambda:self.clock)
    def save_config(self):
        (self.home/'config/turbines.json').write_text(json.dumps(self.config))
    def save_meta(self):
        (self.home/'artifacts/metadata.json').write_text(json.dumps(self.meta))
    def weather(self,tid,horizon,cache_dir,refresh=False,*,forecast_start,now):
        self.calls.append((tid,forecast_start,refresh))
        return {'frame':pd.DataFrame({'timestamp':pd.date_range(forecast_start,periods=horizon,freq='h'),'wind_speed':[self.speed]*horizon,'temperature':[8.0]*horizon}),
                'current':{'valid_time':self.clock.replace(minute=15,second=0).isoformat(),'wind_speed_10m':4.0,'wind_speed_100m':self.speed,'temperature_2m':8.0},
                'provenance':{'source':'fixture','source_hash':f'fixture-{tid}-{self.speed}','requested_coordinates':self.config['turbines'][str(tid)],
                              'retrieved_at':self.clock.isoformat(),'cache':{'age_seconds':0,'status':'fixture'}}}
    def predict(self,artifacts,tid,frame):
        self.predictions+=1
        return frame[['timestamp']].assign(power=.5,lower=.2,upper=.8)
    def test_default_never_invents_mw_or_hard_zeros_low_wind(self):
        result=self.agent.run(24);p=result['turbines'][0]['points'][0]
        self.assertEqual(p['normalized_power'],.5)
        self.assertIsNone(p['power_mw']);self.assertIsNone(p['energy_mwh'])
        self.assertFalse(result['capacity']['conversion_enabled']);self.assertFalse(result['physics_scenario_enabled'])
        self.assertEqual(result['turbines'][0]['physics_corrections'],0)
        self.assertEqual(result['summary']['normalized_mean_proxy'],.5)
        self.assertTrue(result['model']['age_warning']);self.assertEqual(result['mode'],'live')
        self.assertEqual(result['forecast_start'],'2026-09-23T11:00:00+00:00')
        json.dumps(result,allow_nan=False)
    def test_conversion_requires_both_verified_capacity_and_normalization_evidence(self):
        self.config['capacity_verified']=True;self.save_config()
        self.assertIsNone(self.agent.run(24)['turbines'][0]['points'][0]['power_mw'])
        self.config['power_normalization']='rated_capacity_fraction';self.save_config()
        with self.assertRaisesRegex(ForecastError,'evidence'):
            self.agent.run(24)
        self.config.update(capacity_evidence='fixture-spec',power_normalization_evidence='fixture-definition');self.save_config()
        result=self.agent.run(24)
        self.assertEqual(result['turbines'][0]['points'][0]['power_mw'],1.25)
        self.assertEqual(result['summary']['energy_24h_mwh'],60.0)
    def test_assumed_conversion_is_explicit_and_labeled(self):
        self.config.update(capacity_verified=True,capacity_evidence='user-confirmed 2.5 MW',
                           power_normalization='assumed_rated_capacity_fraction',
                           power_normalization_evidence='assumed fraction; not dataset-verified')
        self.save_config()
        result=self.agent.run(24);point=result['turbines'][0]['points'][0]
        self.assertEqual(point['power_mw'],1.25)
        self.assertEqual(result['capacity']['per_turbine_mw'],2.5)
        self.assertTrue(result['energy_conversion']['enabled'])
        self.assertTrue(result['energy_conversion']['assumed'])
        self.assertFalse(result['energy_conversion']['verified'])
        self.assertIn('assumption', ' '.join(result['warnings']).lower())
    def test_scenario_is_opt_in_and_preserves_raw_predictions(self):
        self.config['physics_scenario']['enabled']=True;self.save_config()
        result=self.agent.run(24);p=result['turbines'][0]['points'][0]
        self.assertEqual(p['normalized_power'],0);self.assertEqual(p['raw_normalized_power'],.5)
        self.assertTrue(any('Exploratory' in x for x in result['warnings']))
        self.assertEqual(result['turbines'][0]['physics_corrections'],72)
        self.assertEqual(calculate_physics_power_baseline(2),0)
        self.assertEqual(calculate_physics_power_baseline(25),1)
        with self.assertRaises(ValueError):calculate_physics_power_baseline(-1)
    def test_cache_reuse_refresh_and_changed_inputs(self):
        first=self.agent.run(24);self.clock+=timedelta(seconds=30)
        reused=self.agent.run(24)
        self.assertTrue(reused['reused']);self.assertEqual(first['issued_at'],reused['issued_at']);self.assertEqual(self.predictions,2)
        refreshed=self.agent.run(24,refresh=True)
        self.assertTrue(refreshed['reused']);self.assertEqual(self.predictions,2)
        self.speed=7;changed=self.agent.run(24)
        self.assertFalse(changed['reused']);self.assertEqual(self.predictions,4)
        self.assertEqual(self.agent.store.get(first['id'])['result']['turbines'][0]['points'][0]['wind_speed'],1)
    def test_invalid_or_stale_inputs_fail_and_are_audited(self):
        original=self.weather
        for failure in ('negative','future-retrieval','old-retrieval','naive','wrong-location','future-current'):
            def invalid(*args,**kwargs):
                p=original(*args,**kwargs)
                if failure=='negative':p['frame']['wind_speed']=-1
                if failure=='future-retrieval':p['provenance']['retrieved_at']=(self.clock+timedelta(seconds=1)).isoformat()
                if failure=='old-retrieval':p['provenance']['retrieved_at']=(self.clock-timedelta(seconds=301)).isoformat()
                if failure=='naive':p['frame']['timestamp']=p['frame']['timestamp'].dt.tz_localize(None)
                if failure=='wrong-location':p['provenance']['requested_coordinates']={'latitude':0,'longitude':0}
                if failure=='future-current':p['current']['valid_time']=(self.clock+timedelta(minutes=16)).isoformat()
                return p
            self.agent.weather_fetch=invalid
            with self.subTest(failure=failure),self.assertRaises(ForecastError):self.agent.run(24)
            self.assertEqual(self.agent.store.list(1)[0]['status'],'failed')
        self.assertEqual(self.agent.status()['status'],'error')
    def test_model_cutoff_or_mutation_is_rejected(self):
        self.meta['model_available_at']='2026-09-23T10:45:00Z';self.save_meta()
        with self.assertRaisesRegex(ForecastError,'cutoff'):self.agent.run(24)
        self.assertEqual(self.calls,[])
        self.meta['model_available_at']='2026-01-31T19:00:00Z';self.save_meta()
        def mutate(*args):
            (self.home/'artifacts/model.bin').write_bytes(b'changed')
            return self.predict(*args)
        self.agent.predict=mutate
        with self.assertRaisesRegex(ForecastError,'changed'):self.agent.run(24)
    def test_acquisition_and_inference_hour_boundary_retry(self):
        for during in ('acquisition','inference'):
            self.clock=NOW;self.calls=[]
            once=[False]
            def weather(*args,**kwargs):
                p=self.weather(*args,**kwargs)
                if during=='acquisition' and not once[0]:
                    self.clock=NOW.replace(hour=11,minute=0,second=1);once[0]=True
                return p
            def predict(*args):
                p=self.predict(*args)
                if during=='inference' and not once[0]:
                    self.clock=NOW.replace(hour=11,minute=0,second=1);once[0]=True
                return p
            self.agent.weather_fetch=weather;self.agent.predict=predict
            result=self.agent.run(24,refresh=True)
            self.assertEqual(result['forecast_start'],'2026-09-23T12:00:00+00:00')
    def test_invalid_parameters_and_missing_capacity(self):
        for horizon in (0,25,True,'24'):
            with self.assertRaises(ForecastError):self.agent.run(horizon)
        self.config.pop('capacity_mw');self.save_config()
        self.assertIsNone(self.agent.run(24)['capacity']['per_turbine_mw'])

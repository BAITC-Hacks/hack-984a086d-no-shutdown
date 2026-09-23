"""Offline current-weather cache and source validation with unittest."""
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from windagent import live_weather as weather
NOW=pd.Timestamp('2026-09-23T10:30:00Z')

def payload():
    hours=pd.date_range('2026-09-23T00:00:00Z',periods=72,freq='h')
    return {'latitude':weather.TURBINES[1][0],'longitude':weather.TURBINES[1][1],'timezone':'GMT','utc_offset_seconds':0,
            'hourly_units':{'wind_speed_100m':'m/s','temperature_2m':'°C'},
            'current_units':{'wind_speed_10m':'m/s','wind_speed_100m':'m/s','temperature_2m':'°C'},
            'current':{'time':'2026-09-23T10:15','wind_speed_10m':4.0,'wind_speed_100m':6.0,'temperature_2m':8.0},
            'hourly':{'time':[x.strftime('%Y-%m-%dT%H:%M') for x in hours],'wind_speed_100m':[6.0]*72,'temperature_2m':[8.0]*72}}

class LiveWeatherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.calls=[];self.body=payload();self.clock=NOW
        def fetch(params):self.calls.append(params);return copy.deepcopy(self.body),'raw-fixture'
        self.addCleanup(patch.stopall)
        patch.object(weather,'_request_json',side_effect=fetch).start()
        patch.object(weather,'_clock_now',side_effect=lambda:self.clock).start()
    def fetch(self,**kwargs):return weather.fetch_live_weather(1,48,self.home,now=self.clock,**kwargs)
    def test_current_and_exact_future_hours_have_explicit_provenance(self):
        result=self.fetch()
        self.assertEqual(len(result['frame']),48)
        self.assertEqual(result['frame'].timestamp.iloc[0],pd.Timestamp('2026-09-23T11:00:00Z'))
        self.assertEqual(result['frame'].timestamp.iloc[-1],pd.Timestamp('2026-09-25T10:00:00Z'))
        self.assertEqual(result['provenance']['availability_basis'],'actual_retrieval')
        self.assertIsNone(result['provenance']['initialization_time'])
        self.assertTrue(result['provenance']['current_is_model_estimate'])
        self.assertEqual(self.calls[0]['models'],'ecmwf_ifs')
    def test_ttl_corruption_and_old_schema_trigger_new_acquisition(self):
        self.fetch();self.clock+=pd.Timedelta(seconds=30)
        hit=self.fetch();self.assertEqual(len(self.calls),1);self.assertEqual(hit['provenance']['cache']['age_seconds'],30)
        path=next(self.home.rglob('*.json'));record=json.loads(path.read_text())
        record['schema_version']=1;path.write_text(json.dumps(record));self.fetch();self.assertEqual(len(self.calls),2)
        record=json.loads(path.read_text());record['response']['hourly']['wind_speed_100m'][12]=99;path.write_text(json.dumps(record))
        self.fetch();self.assertEqual(len(self.calls),3)
        self.clock+=pd.Timedelta(seconds=301);self.fetch();self.assertEqual(len(self.calls),4)
        self.fetch(refresh=True);self.assertEqual(len(self.calls),5)
    def test_provider_processing_time_does_not_change_weather_identity(self):
        first=self.fetch();self.body['generationtime_ms']=999.0
        second=self.fetch(refresh=True)
        self.assertEqual(first['provenance']['source_hash'],second['provenance']['source_hash'])
    def test_units_coordinates_ranges_timestamps_and_nonfinite_fail(self):
        original=payload()
        def remove_hour(p):
            for col in ('time','wind_speed_100m','temperature_2m'):p['hourly'][col].pop(14)
        variants=[lambda p:p['hourly_units'].update(wind_speed_100m='km/h'),lambda p:p.update(latitude=0),
            lambda p:p['current'].update(wind_speed_100m=-1),lambda p:p['current'].update(temperature_2m=71),
            lambda p:p['current'].update(time='2026-09-23T11:00'),lambda p:p['current'].update(time='2026-09-23T09:00'),
            lambda p:p['hourly']['wind_speed_100m'].__setitem__(14,float('nan')),
            lambda p:p['hourly']['temperature_2m'].__setitem__(14,-100),remove_hour]
        for change in variants:
            self.body=copy.deepcopy(original);change(self.body)
            with self.subTest(change=change),self.assertRaises(weather.LiveWeatherUnavailableError):self.fetch(refresh=True)
    def test_invalid_inputs_never_make_requests(self):
        for horizon in (25,False,'24'):
            with self.assertRaises(ValueError):weather.fetch_live_weather(1,horizon,self.home,now=NOW)
        with self.assertRaises(ValueError):weather.fetch_live_weather(1,24,self.home,now='2026-09-23T10:30')
        with self.assertRaises(ValueError):self.fetch(forecast_start='2026-09-23T12:00:00Z')
        self.assertEqual(self.calls,[])

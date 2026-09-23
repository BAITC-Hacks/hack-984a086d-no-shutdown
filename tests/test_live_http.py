"""Actual HTTP handler integration without sockets or weather network calls."""
from __future__ import annotations
import csv,io,json,os,subprocess,sys,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
from windagent import service,chat
from windagent.agent import ForecastAgent
from windagent.live import LiveForecastAgent
from windagent.server import Handler
ROOT=Path(__file__).resolve().parents[1]
NOW=datetime(2026,9,23,10,30,tzinfo=timezone.utc)

class LiveHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);(self.home/'artifacts').mkdir();(self.home/'config').mkdir()
        self.config=json.loads((ROOT/'config/turbines.json').read_text())
        self.config.update(capacity_verified=False,capacity_evidence=None,
                           power_normalization='unknown',power_normalization_evidence=None)
        (self.home/'config/turbines.json').write_text(json.dumps(self.config))
        (self.home/'artifacts/metadata.json').write_text(json.dumps({'model_version':'http-fixture','model_available_at':'2026-01-31T19:00:00Z','timezone':'Asia/Almaty',
            'turbines':{str(t):{'training_last_hour_utc':'2026-01-31T18:00:00Z','holdout':{'mae':.02}} for t in (1,2)}}))
        (self.home/'artifacts/model.bin').write_bytes(b'fixture')
        self.acquisitions=[]
        def weather(tid,horizon,cache_dir,refresh=False,*,forecast_start,now):
            self.acquisitions.append((tid,horizon,refresh))
            return {'frame':pd.DataFrame({'timestamp':pd.date_range(forecast_start,periods=horizon,freq='h'),'wind_speed':[float(4+tid)]*horizon,'temperature':[8.0]*horizon}),
                    'current':{'valid_time':NOW.replace(minute=15).isoformat(),'wind_speed_10m':4.0,'wind_speed_100m':float(4+tid),'temperature_2m':8.0},
                    'provenance':{'source':'fixture live weather','model':'fixture model','source_hash':f'http-{tid}','retrieved_at':NOW.isoformat(),
                                  'requested_coordinates':self.config['turbines'][str(tid)],'cache':{'age_seconds':0}}}
        def predict(artifacts,tid,frame):return frame[['timestamp']].assign(power=.2 if tid==1 else .7,lower=.1,upper=.9)
        self.live=LiveForecastAgent(self.home,weather_fetch=weather,predict=predict,now=lambda:NOW)
        def forbidden(*args,**kwargs):raise AssertionError('Historical acquisition must not be called by live routes')
        self.historical=ForecastAgent(self.home,weather_fetch=forbidden,predict=predict,strict_as_of=False)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,{'WINDAGENT_HOME':str(self.home),'WINDAGENT_CHAT_PROVIDER':'local'}).start()
        patch.object(service,'get_agent',return_value=self.historical).start()
        patch.object(chat,'get_agent',return_value=self.historical).start()
        patch.object(service,'get_live_agent',return_value=self.live).start()
    def request(self,path,*,method='GET',payload=None,headers=None):
        class QuietHandler(Handler):
            def log_message(self,*args):pass
        class Connection:
            def __init__(self,raw):self.input=io.BytesIO(raw);self.output=bytearray()
            def makefile(self,*args):return self.input
            def sendall(self,data):self.output.extend(data)
        body=json.dumps(payload).encode() if payload is not None else b''
        chosen={'Host':'127.0.0.1:8000','Connection':'close'}
        if method=='POST':chosen.update({'Content-Type':'application/json','Content-Length':str(len(body))})
        chosen.update(headers or {})
        wire=(f'{method} {path} HTTP/1.1\r\n'+''.join(f'{k}: {v}\r\n' for k,v in chosen.items())+'\r\n').encode()+body
        connection=Connection(wire)
        QuietHandler(connection,('127.0.0.1',32100),SimpleNamespace(server_name='localhost',server_port=8000))
        header,raw=bytes(connection.output).split(b'\r\n\r\n',1)
        status=int(header.split(b' ',2)[1])
        values=dict(line.decode().split(': ',1) for line in header.split(b'\r\n')[1:])
        self.assertEqual(int(values['Content-Length']),len(raw))
        return status,values,raw
    def json_request(self,path,**kwargs):
        status,headers,body=self.request(path,**kwargs)
        return status,json.loads(body)
    def test_live_dashboard_uses_selected_turbine_and_current_origin(self):
        status,data=self.json_request('/api/forecast?mode=live&turbine_id=T2&as_of_date=2026-02-01&horizon_hours=24')
        self.assertEqual(status,200,data);self.assertEqual(data['mode'],'live')
        self.assertEqual(data['as_of_date'],'2026-09-23');self.assertEqual(data['origin'],'2026-09-23T11:00:00+00:00')
        self.assertEqual(data['turbine_id'],'T2');self.assertEqual(len(data['forecast']),24)
        self.assertEqual(data['forecast'][0]['predicted_power'],.7)
        self.assertEqual(data['forecast'][0]['wind_speed'],6.0)
        self.assertEqual(data['fleet_summary']['T1']['peak'],.2)
        self.assertEqual(data['fleet_summary']['T2']['peak'],.7)
        self.assertEqual(data['power_unit'],'normalized');self.assertIsNone(data['capacity_mw'])
        self.assertEqual(len(self.acquisitions),2)
        self.assertEqual(data['model']['version'],'http-fixture')
    def test_chat_uses_selected_live_context_and_cross_turbine_tools(self):
        base={'message':'Какой пик мощности?','mode':'live','turbine_id':'T2','as_of_date':'2026-02-01','horizon_hours':24}
        status,answer=self.json_request('/api/chat',method='POST',payload=base)
        self.assertEqual(status,200,answer);self.assertEqual(answer['context']['mode'],'live')
        self.assertEqual(answer['context']['turbine_id'],'T2');self.assertEqual(answer['context']['as_of_date'],'2026-09-23')
        self.assertIn('0.700',answer['reply']);self.assertIn('текущий прогноз',answer['reply'])
        self.assertNotIn('Историческая доступность',answer['reply'])
        status,comparison=self.json_request('/api/chat',method='POST',payload={**base,'message':'Сравни турбины'})
        self.assertEqual(status,200,comparison);self.assertIn('0.200',comparison['reply']);self.assertIn('0.700',comparison['reply'])
        status,limits=self.json_request('/api/chat',method='POST',payload={**base,'message':'Проверь ограничения'})
        self.assertEqual(status,200);self.assertIn('не архив февраля',limits['reply']);self.assertIn('не измерения SCADA',limits['reply'])
    def test_raw_forecast_status_persistence_and_live_csv(self):
        status,raw=self.json_request('/live/forecast?horizon=48')
        self.assertEqual(status,200,raw);self.assertEqual(raw['mode'],'live')
        self.assertEqual(raw['summary']['power_unit'],'normalized');self.assertIsNone(raw['summary']['energy_24h_mwh'])
        self.assertFalse(raw['capacity']['conversion_enabled'])
        self.assertEqual(len(raw['turbines'][0]['points']),48)
        status,state=self.json_request('/live/status')
        self.assertEqual(status,200,state);self.assertEqual(state['status'],'ok');self.assertFalse(state['background_loop_enabled'])
        self.assertEqual(state['last_successful_issue_at'],raw['issued_at'])
        status,record=self.json_request(f'/forecasts/{raw["id"]}')
        self.assertEqual(status,200,record);self.assertEqual(record['result']['mode'],'live')
        self.assertTrue(record['audit'])
        status,headers,body=self.request(f'/forecasts/{raw["id"]}/csv')
        self.assertEqual(status,200,body)
        rows=list(csv.DictReader(io.StringIO(body.decode())))
        self.assertEqual(len(rows),96)
        self.assertEqual(float(rows[0]['normalized_power']),.2)
        self.assertEqual(float(rows[48]['normalized_power']),.7)
        self.assertNotIn('power_mw',rows[0])
    def test_live_alias_returns_assumption_labeled_farm_summary(self):
        self.config.update(capacity_verified=True,capacity_evidence='user-confirmed 2.5 MW',
                           power_normalization='assumed_rated_capacity_fraction',
                           power_normalization_evidence='assumed fraction; not dataset-verified')
        (self.home/'config/turbines.json').write_text(json.dumps(self.config))
        status,raw=self.json_request('/api/live?hours=24')
        self.assertEqual(status,200,raw)
        self.assertTrue(raw['energy_conversion']['enabled'])
        self.assertTrue(raw['energy_conversion']['assumed'])
        self.assertFalse(raw['energy_conversion']['verified'])
        self.assertEqual(raw['capacity']['per_turbine_mw'],2.5)
        self.assertAlmostEqual(raw['farm']['points'][0]['power_mw'],raw['turbines'][0]['points'][0]['power_mw']+raw['turbines'][1]['points'][0]['power_mw'])
        status,legacy=self.json_request('/api/live/status')
        self.assertEqual(status,200,legacy)
        self.assertIn('background_loop_enabled',legacy)
    def test_photos_are_actual_allowlisted_jpeg_bytes(self):
        for name in ('wind-night.jpg','energy-grid.jpg','operator.jpg'):
            status,headers,body=self.request('/assets/'+name)
            self.assertEqual(status,200,name)
            self.assertTrue(headers['Content-Type'].startswith('image/jpeg'))
            self.assertEqual(body,(ROOT/'assets'/name).read_bytes())
            self.assertTrue(body.startswith(b'\xff\xd8\xff'),name)
        status,headers,body=self.request('/assets/power_curves_turbines.png')
        self.assertEqual(status,200)
        self.assertTrue(headers['Content-Type'].startswith('image/png'))
        self.assertTrue(body.startswith(b'\x89PNG\r\n\x1a\n'))
        for path in ('/.env','/assets/../.env','/assets/%2e%2e/.env','/%2e%2e/config/turbines.json','/assets/private.jpg','/config/turbines.json'):
            self.assertEqual(self.request(path)[0],404,path)
    def test_malformed_modes_and_horizons_are_rejected(self):
        for path in ('/api/forecast?mode=other&turbine_id=T1','/api/forecast?mode=live&turbine_id=T3',
                     '/live/forecast?horizon=25','/api/forecast?mode=live&turbine_id=T1&refresh=maybe'):
            self.assertEqual(self.request(path)[0],422,path)
        status,data=self.json_request('/api/chat',method='POST',payload={'message':'hi','mode':'invented'})
        self.assertEqual(status,422,data)
        self.assertEqual(self.acquisitions,[])
    def test_status_capabilities_and_cli_help(self):
        status,data=self.json_request('/api/status')
        self.assertEqual(status,200,data);self.assertTrue(data['capabilities']['live'])
        self.assertIn('compare_turbines',data['capabilities']['chat_tools']);self.assertIn('best_window',data['capabilities']['chat_tools'])
        self.assertEqual(data['chat']['mode'],'local')
        completed=subprocess.run([sys.executable,'-m','windagent','live','--help'],cwd=ROOT,text=True,capture_output=True,timeout=30)
        self.assertEqual(completed.returncode,0,completed.stderr);self.assertIn('--horizon',completed.stdout)
        self.assertIn('--csv',completed.stdout)

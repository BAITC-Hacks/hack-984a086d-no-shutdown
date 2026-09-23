"""Run without pytest: python scripts/verify_service.py [--real].

Uses a fixture model for service assertions. --real also loads the delivered
trained artifacts and archive, without any external network requests.
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windagent import service, chat
from windagent.agent import ForecastAgent, ForecastError

RUN_REAL = False


def fixture_weather(turbine_id, origin, horizon, cache_dir, refresh=False):
    origin = pd.Timestamp(origin).tz_convert('UTC')
    return pd.DataFrame({
        'timestamp': pd.date_range(origin, periods=horizon, freq='h'),
        'wind_speed': [6.0] * horizon, 'temperature': [-4.0] * horizon,
        'initialized_at': [origin - pd.Timedelta(hours=18)] * horizon,
        'available_at': [origin - pd.Timedelta(hours=6)] * horizon,
        'source': ['test fixture'] * horizon, 'source_hash': [f'fixture-{turbine_id}'] * horizon,
    })


def fixture_predict(artifact_dir, turbine_id, frame):
    return frame[['timestamp']].assign(power=0.5, lower=0.3, upper=0.7)


class ServiceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        artifacts = self.home / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'metadata.json').write_text(json.dumps({
            'model_version': 'fixture-model', 'model_available_at': '2026-01-31T19:00:00Z',
            'timezone': 'Asia/Almaty', 'turbines': {
                '1': {'training_last_hour_utc': '2026-01-31T18:00:00Z', 'holdout': {'mae': 0.1234}},
                '2': {'training_last_hour_utc': '2026-01-31T18:00:00Z', 'holdout': {'mae': 0.2345}},
            }
        }))
        (artifacts / 'model.bin').write_bytes(b'fixture')
        self.calls = 0
        def counted_weather(*args, **kwargs):
            self.calls += 1
            return fixture_weather(*args, **kwargs)
        self.agent = ForecastAgent(self.home, weather_fetch=counted_weather, predict=fixture_predict, strict_as_of=False)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'WINDAGENT_CHAT_PROVIDER': 'local', 'WINDAGENT_HOME': str(self.home)}).start()

    def request(self, **kwargs):
        values = dict(turbine_id='T1', as_of_date='2026-02-01', horizon_hours=24)
        values.update(kwargs)
        return service.dashboard_forecast(**values, agent=self.agent)

    def test_dashboard_first_calculation_and_reuse_are_json_safe(self):
        first = self.request()
        self.assertEqual(self.calls, 2)
        self.assertEqual(first['forecast'][0]['wind_speed'], 6.0)
        self.assertEqual(set(first['forecast'][0]), {'timestamp', 'predicted_power', 'lower', 'upper', 'wind_speed', 'temperature'})
        self.assertEqual(first['provenance']['source'], ['test fixture'])
        self.assertEqual(first['power_unit'], 'normalized')
        self.assertIsNone(first['capacity_mw'])
        self.assertFalse(first['as_of_verified'])
        self.assertIn('МВт', ' '.join(first['warnings']))
        self.assertEqual(len(first['forecast']), 24)
        second = self.request()
        self.assertEqual(first['forecast'], second['forecast'])
        self.assertNotEqual(first['forecast_id'], second['forecast_id'])
        json.dumps(first, allow_nan=False)
        json.dumps(second, allow_nan=False)

    def test_invalid_parameters_are_actionable(self):
        for kwargs in (
            {'turbine_id': 'T3'}, {'turbine_id': None}, {'as_of_date': '2026-02-30'},
            {'as_of_date': '2026-2-1'}, {'as_of_date': None}, {'horizon_hours': 25},
            {'horizon_hours': True}, {'horizon_hours': 24.5}, {'refresh': 'sometimes'},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(service.ServiceError):
                self.request(**kwargs)
        with self.assertRaises(ForecastError):
            self.request(as_of_date='2026-01-31')

    def test_horizon_and_boolean_validation(self):
        for value in (24, '24', 48, '48'):
            self.assertIn(service.horizon_value(value), (24,48))
        for value in (None, [], {}, 0, 12, 24.1, '24.0', True):
            with self.subTest(value=value), self.assertRaises(service.ServiceError):
                service.horizon_value(value)
        for value in (False, 'false', '0'):
            self.assertFalse(service.boolean_value(value))
        for value in (True, 'true', '1'):
            self.assertTrue(service.boolean_value(value))

    def test_csv_export_and_status(self):
        result = self.request()
        record = self.agent.store.get(result['forecast_id'])
        exported = service.csv_content(record)
        self.assertEqual(len(exported.strip().splitlines()), 49)
        self.assertIn('normalized_power', exported.splitlines()[0])
        for record, status in ((None,404), ({'status':'failed'},409)):
            with self.assertRaises(service.ServiceError) as caught:
                service.csv_content(record)
            self.assertEqual(caught.exception.status,status)
        status = service.status(agent=self.agent)
        self.assertFalse(status['strict_as_of'])
        self.assertEqual(status['chat']['mode'], 'local')
        json.dumps(status, allow_nan=False)

    def test_chat_is_grounded_and_honest_about_local_mode(self):
        def ask(message):
            return chat.answer({'message':message, 'turbine_id':'T1', 'as_of_date':'2026-02-01', 'horizon_hours':24},agent=self.agent)
        summary = ask('Какой пик мощности?')
        self.assertIn('0.500', summary['reply'])
        self.assertEqual(summary['mode'], 'local')
        self.assertFalse(summary['context']['as_of_verified'])
        self.assertIn('не подтверждена', summary['reply'])
        self.assertIn('6.0', ask('Расскажи про ветер')['reply'])
        self.assertIn('0.1234', ask('Какая точность?')['reply'])
        self.assertIn('не точность прогноза на 48 часов', ask('Какая точность?')['reply'])
        self.assertIn('as_of_verified=false', ask('Проверь утечку')['reply'])
        training_reply = ask('Переобучи модель')['reply']
        self.assertRegex(training_reply, r'Чат (?:сам )?не (?:меняет|изменяет)')
        self.assertIn('артефакты', training_reply)
        self.assertIn('локальный аналитик', ask('Расскажи рецепт пиццы')['reply'])
        json.dumps(summary, allow_nan=False)

    def test_chat_rejects_malformed_messages(self):
        for payload in ({}, {'message':''}, {'message':'  '}, {'message':42}, {'message':'a'*4001},
                        {'message':'hi','history':{}}, {'message':'hi','history':[{}]*31},
                        {'message':'hi','turbine_id':'T3'}):
            with self.subTest(payload=str(payload)[:80]), self.assertRaises(service.ServiceError):
                chat.answer(payload,agent=self.agent)

    def test_optional_provider_failure_falls_back_visibly(self):
        with patch.dict(os.environ, {'WINDAGENT_CHAT_PROVIDER':'openai', 'OPENAI_MODEL':'test-model', 'OPENAI_API_KEY':'test-key'}):
            with patch.object(chat, '_openai_reply', side_effect=OSError('offline')):
                reply = chat.answer({'message':'Какой прогноз?', 'horizon_hours':24}, agent=self.agent)
                self.assertEqual(reply['mode'],'local')
                self.assertIn('недоступна',reply['warning'])
                self.assertIn('0.500',reply['reply'])

    def test_http_handler_routes_validation_and_static_allowlist(self):
        """Exercise actual request parsing without opening a forbidden socket."""
        from windagent.server import Handler
        class QuietHandler(Handler):
            def log_message(self, *args):
                pass
        class MemoryConnection:
            def __init__(self, raw):
                self.input = io.BytesIO(raw)
                self.output = bytearray()
            def makefile(self, mode, *args):
                return self.input
            def sendall(self, data):
                self.output.extend(data)
        def request(path, *, method='GET', payload=None, raw_body=None, headers=None):
            body = json.dumps(payload).encode() if payload is not None else raw_body or b''
            selected = {'Host':'127.0.0.1:8000', 'Connection':'close'}
            if method == 'POST':
                selected.update({'Content-Type':'application/json', 'Content-Length':str(len(body))})
            selected.update(headers or {})
            raw = (f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in selected.items())+'\r\n').encode()+body
            connection = MemoryConnection(raw)
            QuietHandler(connection, ('127.0.0.1',12345), SimpleNamespace(server_name='localhost',server_port=8000))
            header, response = bytes(connection.output).split(b'\r\n\r\n',1)
            status=int(header.split(b' ',2)[1])
            return status,header,response
        with patch.object(service,'get_agent',return_value=self.agent), patch.object(chat,'get_agent',return_value=self.agent):
            for path in ('/','/styles.css','/app.js'):
                status, headers, body = request(path)
                self.assertEqual(status,200,path)
                self.assertGreater(len(body),10)
                self.assertIn(b'nosniff',headers)
            for path in ('/.env','/../README.md','/%2e%2e/README.md','/artifacts/metadata.json',
                         '/data/raw/turbine_1.csv','/windagent/server.py','/state/windagent.sqlite3'):
                self.assertEqual(request(path)[0],404,path)
            self.assertEqual(request('/health')[0],200)
            self.assertEqual(request('/api/status')[0],200)
            self.assertEqual(request('/api/forecast?turbine_id=T3&as_of_date=2026-02-01')[0],422)
            self.assertEqual(request('/forecasts?limit=abc')[0],422)
            status, _, body = request('/api/forecast?turbine_id=T2&as_of_date=2026-02-01&horizon_hours=24')
            self.assertEqual(status,200,body)
            forecast=json.loads(body)
            self.assertEqual(len(forecast['forecast']),24)
            self.assertFalse(forecast['as_of_verified'])
            forecast_id=forecast['forecast_id']
            self.assertEqual(request(f'/forecasts/{forecast_id}')[0],200)
            status,_,csv=request(f'/forecasts/{forecast_id}/csv')
            self.assertEqual(status,200)
            self.assertEqual(len(csv.strip().splitlines()),49)
            self.assertEqual(request('/forecasts/99999')[0],404)
            status,_,body=request('/api/chat',method='POST',payload={'message':'Какой прогноз?','horizon_hours':24})
            self.assertEqual(status,200,body)
            self.assertEqual(json.loads(body)['mode'],'local')
            self.assertEqual(request('/api/chat',method='POST',payload=[])[0],422)
            self.assertEqual(request('/api/chat',method='POST',raw_body=b'{broken')[0],422)
            self.assertEqual(request('/api/chat',method='POST',payload={},headers={'Content-Type':'text/plain'})[0],415)
            self.assertEqual(request('/api/chat',method='POST',payload={'message':'hello'},headers={'Origin':'https://example.com'})[0],403)
            status,_,body=request('/forecasts',method='POST',payload={'origin':'2026-02-01T00:00:00+05:00','horizon':24})
            self.assertEqual(status,200,body)
            self.assertEqual(request('/forecasts',method='POST',payload={'origin':'bad-time','horizon':24})[0],422)
            self.assertEqual(request('/forecasts',method='POST',payload={'origin':'2026-02-01T00:00:00+05:00','horizon':25})[0],422)

    def test_socket_http_smoke_environment_blocked(self):
        self.skipTest('Root server bind was denied by sandbox (Operation not permitted); socket test not retried. Actual Handler is tested in memory.')

    def test_real_trained_forecast_offline(self):
        if not RUN_REAL:
            self.skipTest('Use --real after training completes')
        shutil.copytree(ROOT/'artifacts',self.home/'artifacts',dirs_exist_ok=True)
        shutil.copytree(ROOT/'data/weather',self.home/'data/weather')
        def no_network(*args,**kwargs):
            raise AssertionError('Unexpected external request in real offline test')
        with patch('windagent.weather.urlopen',no_network):
            agent = ForecastAgent(self.home, strict_as_of=False)
            result = service.dashboard_forecast('T2','2026-02-01',48,agent=agent)
            self.assertEqual(len(result['forecast']),48)
            self.assertFalse(result['as_of_verified'])
            for row in result['forecast']:
                self.assertTrue(0 <= row['lower'] <= row['predicted_power'] <= row['upper'] <= 1)
            json.dumps(result,allow_nan=False)
            reply = chat.answer({'message':'Объясни прогноз','turbine_id':'T2','horizon_hours':24},agent=agent)
            self.assertEqual(reply['mode'],'local')
            json.dumps(reply,allow_nan=False)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--real',action='store_true')
    args=parser.parse_args()
    RUN_REAL=args.real
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(ServiceChecks)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    report={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'skipped':[{'test':str(test),'reason':reason} for test,reason in result.skipped],
            'passed':result.wasSuccessful(),'real_model_requested':RUN_REAL,
            'framework':'Python unittest (no pytest dependency)'}
    report['http_transport']='Actual BaseHTTPRequestHandler parsing with in-memory connection; socket bind unavailable'
    (ROOT/'reports/service_verification.json').write_text(json.dumps(report,indent=2))
    sys.exit(0 if result.wasSuccessful() else 1)

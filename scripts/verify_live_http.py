"""Check live API integration through actual Handler parsing, without sockets."""
import importlib.util,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
if __name__=='__main__':
    spec=importlib.util.spec_from_file_location('test_live_http',ROOT/'tests/test_live_http.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    report={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'passed':result.wasSuccessful(),
            'transport':'Actual HTTP Handler with in-memory connection; no socket bind',
            'orchestration':'Real LiveForecastAgent with injected deterministic weather/model; actual service/chat/CSV routes',
            'external_network_requested':False,'live_weather_acquisition':'not_run','cli_help':'included in tests'}
    (ROOT/'reports/live_http_verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2));sys.exit(0 if result.wasSuccessful() else 1)

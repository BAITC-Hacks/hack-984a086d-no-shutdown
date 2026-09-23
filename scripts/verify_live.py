"""Offline live-feature regressions; no real weather requests or sockets."""
import importlib.util,json,shutil,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
def load_file(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module);return module
if __name__=='__main__':
    suite=unittest.TestSuite();modules=[]
    for name in ('test_live','test_live_weather'):
        module=load_file(name,ROOT/'tests'/f'{name}.py');modules.append(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    real_model='not_run'
    if result.wasSuccessful():
        fixture=modules[0].LiveAgentTests(methodName='test_default_never_invents_mw_or_hard_zeros_low_wind');fixture.setUp()
        try:
            shutil.copytree(ROOT/'artifacts',fixture.home/'artifacts',dirs_exist_ok=True)
            from windagent.model import predict_power
            fixture.agent.predict=predict_power;output=fixture.agent.run(48)
            assert output['model']['version']=='conditional-power-v2'
            assert output['capacity']['conversion_enabled'] is False
            for turbine in output['turbines']:
                assert len(turbine['points'])==48
                for point in turbine['points']:
                    assert 0<=point['lower_normalized']<=point['normalized_power']<=point['upper_normalized']<=1
                    assert point['power_mw'] is None
            json.dumps(output,allow_nan=False)
            real_model='passed: delivered v2 artifacts with deterministic weather fixture (not live acquisition)'
        finally:fixture.doCleanups()
    report={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'passed':result.wasSuccessful(),
            'real_model_integration':real_model,'external_network_requested':False,
            'live_api_acquisition':'not_run: fixtures validate transport logic; no live success claimed',
            'default_units':'normalized','capacity_verified':False,'physics_scenario_enabled':False}
    (ROOT/'reports/live_verification.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
    sys.exit(0 if result.wasSuccessful() else 1)

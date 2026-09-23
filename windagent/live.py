"""Current-weather forecasting using the existing trained conditional model.

Live and historical replay are separate; assumed unit conversion and the
explicit power-curve scenario remain visible in each result.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from .agent import ForecastError, _iso
from .model import predict_power
from .physics import validate_physics_predictions, validate_thresholds
from .storage import ForecastStore

PIPELINE_VERSION = 'live-v2-assumption-labeled'
_STATUS_LOCK = threading.Lock()
_STATUS = {'last_successful_issue_at':None,'last_check_at':None,'error':None}


def _utcnow():
    return datetime.now(timezone.utc)


def _timestamp(value,field):
    try:
        stamp=pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError('timezone-aware timestamp required')
        return stamp.tz_convert('UTC').to_pydatetime()
    except (ValueError,TypeError,OverflowError) as exc:
        raise ForecastError(f'Invalid timezone-aware {field}') from exc


def _ceil_hour(value):
    return _timestamp(value,'clock').replace(minute=0,second=0,microsecond=0)+timedelta(hours=1)


def _hash_json(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False,default=str).encode()).hexdigest()


def _pipeline_code_hash():
    """Invalidate reusable forecasts whenever prediction logic changes."""
    digest = hashlib.sha256()
    for name in ('live.py', 'model.py', 'physics.py', 'live_weather.py'):
        path = Path(__file__).with_name(name)
        digest.update(name.encode())
        digest.update(path.read_bytes())
    research_physics = Path(__file__).resolve().parents[1] / 'src' / 'physics.py'
    if research_physics.is_file():
        digest.update(b'src/physics.py')
        digest.update(research_physics.read_bytes())
    return digest.hexdigest()


def _apply_physics_scenario(frame, pred_col, enabled, cut_in, cut_out):
    if enabled and cut_in == 2.5 and cut_out == 25.0:
        try:
            from src.physics import apply_physics_sanity_check
            return apply_physics_sanity_check(frame, pred_col=pred_col, wind_col='wind_speed')
        except ImportError:
            pass
    return validate_physics_predictions(frame, pred_col=pred_col, enabled=enabled,
                                        cut_in=cut_in, cut_out=cut_out)


def _number(value,label,low,high):
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or not low<=value<=high:
        raise ForecastError(f'{label} must be a finite number in [{low}, {high}]')
    return float(value)


class LiveForecastAgent:
    def __init__(self,home:Path,*,weather_fetch:Callable|None=None,predict:Callable|None=None,now:Callable=_utcnow):
        self.home=Path(home).resolve()
        self.artifact_dir=self.home/'artifacts'
        self.cache_dir=self.home/'data/live_weather'
        self.config_path=self.home/'config/turbines.json'
        self.store=ForecastStore(self.home/'state/windagent.sqlite3')
        self.now=now
        self.predict=predict or predict_power
        if weather_fetch is None:
            from .live_weather import fetch_live_weather
            weather_fetch=fetch_live_weather
        self.weather_fetch=weather_fetch
        self._run_lock=threading.Lock()
        self._status={'last_successful_issue_at':None,'last_check_at':None,'error':None}

    def _configuration(self):
        try:
            config=json.loads(self.config_path.read_text(encoding='utf-8-sig'))
        except (OSError,ValueError) as exc:
            raise ForecastError(f'Cannot read turbine configuration: {exc}') from exc
        turbines=config.get('turbines',{})
        if not isinstance(turbines,dict) or set(turbines)!={'1','2'}:
            raise ForecastError('Configure coordinates for turbines 1 and 2')
        for key,point in turbines.items():
            if not isinstance(point,dict):
                raise ForecastError(f'Invalid turbine {key} coordinates')
            _number(point.get('latitude'),f'Turbine {key} latitude',-90,90)
            _number(point.get('longitude'),f'Turbine {key} longitude',-180,180)
        capacity=config.get('capacity_mw')
        if capacity is not None:
            _number(capacity,'capacity_mw',0.000001,1000)
        verified=config.get('capacity_verified',False)
        if not isinstance(verified,bool):
            raise ForecastError('capacity_verified must be boolean')
        normalization=config.get('power_normalization')
        conversion_verified=verified and normalization=='rated_capacity_fraction'
        conversion_assumed=verified and normalization=='assumed_rated_capacity_fraction'
        enabled=conversion_verified or conversion_assumed
        if enabled and (capacity is None or any(not isinstance(config.get(k),str) or not config[k].strip()
                                                for k in ('capacity_evidence','power_normalization_evidence'))):
            raise ForecastError('MW conversion requires capacity and normalization evidence/assumption references')
        scenario=config.get('physics_scenario',{'enabled':False})
        if not isinstance(scenario,dict) or not isinstance(scenario.get('enabled',False),bool):
            raise ForecastError('physics_scenario.enabled must be boolean')
        validate_thresholds(scenario.get('cut_in_speed',2.5),scenario.get('rated_speed',10.5),scenario.get('cut_out_speed',25.0))
        return config,enabled,conversion_verified,conversion_assumed

    def _model_metadata(self,checked_at):
        try:
            metadata=json.loads((self.artifact_dir/'metadata.json').read_text(encoding='utf-8-sig'))
        except (OSError,ValueError) as exc:
            raise ForecastError(f'Cannot read trained model metadata: {exc}') from exc
        cutoff=_timestamp(metadata.get('model_available_at'),'model availability cutoff')
        trained=[]
        for key in ('1','2'):
            record=metadata.get('turbines',{}).get(key,{})
            end=_timestamp(record.get('training_last_hour_utc'),'training_last_hour_utc')+timedelta(hours=1)
            if end>cutoff or cutoff>checked_at:
                raise ForecastError('Model training/availability cutoff is after the live issue time')
            trained.append(end)
        code_hash=_pipeline_code_hash()
        digest=hashlib.sha256(PIPELINE_VERSION.encode()+code_hash.encode())
        for path in sorted(p for p in self.artifact_dir.rglob('*') if p.is_file()):
            digest.update(path.relative_to(self.artifact_dir).as_posix().encode());digest.update(path.read_bytes())
        return metadata,digest.hexdigest(),max(trained),code_hash

    @staticmethod
    def _validate_weather(payload,origin,hours,turbine_id,checked_at):
        if not isinstance(payload,dict) or not isinstance(payload.get('frame'),pd.DataFrame):
            raise ForecastError(f'Turbine {turbine_id}: weather must contain a DataFrame')
        frame=payload['frame'].copy()
        if {'timestamp','wind_speed','temperature'}-set(frame.columns):
            raise ForecastError('Live weather is missing required columns')
        stamps=[_timestamp(v,'weather timestamp') for v in frame.timestamp]
        times=pd.to_datetime(stamps,utc=True)
        expected=pd.date_range(origin,periods=hours,freq='h',tz='UTC')
        if len(frame)!=hours or not times.equals(expected):
            raise ForecastError('Live weather must contain exactly consecutive hours from forecast_start')
        frame['timestamp']=times
        for column,low,high in (('wind_speed',0,100),('temperature',-90,70)):
            values=pd.to_numeric(frame[column],errors='coerce')
            if not values.between(low,high).all() or not values.map(math.isfinite).all():
                raise ForecastError(f'Live weather {column} is missing, negative or implausible')
            frame[column]=values.astype(float)
        current=payload.get('current');provenance=payload.get('provenance')
        if not isinstance(current,dict) or not isinstance(provenance,dict):
            raise ForecastError('Live weather lacks current/provenance metadata')
        current_time=_timestamp(current.get('valid_time'),'current valid_time')
        if not checked_at-timedelta(hours=1)<=current_time<=checked_at+timedelta(minutes=15):
            raise ForecastError('Current model estimate is stale or too far in the future')
        current=dict(current,valid_time=current_time.isoformat())
        for field,low,high in (('wind_speed_10m',0,100),('wind_speed_100m',0,100),('temperature_2m',-90,70)):
            current[field]=_number(current.get(field),field,low,high)
        retrieved=_timestamp(provenance.get('retrieved_at'),'retrieved_at')
        real_age=(checked_at-retrieved).total_seconds()
        recorded_age=provenance.get('cache',{}).get('age_seconds')
        if isinstance(recorded_age,bool) or not isinstance(recorded_age,(int,float)) or not math.isfinite(recorded_age) or not 0<=recorded_age<=300 or not 0<=real_age<=300:
            raise ForecastError('Live weather cache is stale or has a future retrieval time')
        if not all(isinstance(provenance.get(key),str) and provenance[key].strip() for key in ('source','source_hash')):
            raise ForecastError('Live weather lacks source provenance')
        return frame.reset_index(drop=True),current,dict(provenance)

    def _set_status(self,issue,error):
        with _STATUS_LOCK:
            update={'last_check_at':_timestamp(self.now(),'clock').isoformat(),'error':error}
            if issue:
                update['last_successful_issue_at']=issue
            self._status.update(update);_STATUS.update(update)

    def status(self,background_loop_enabled=False):
        with _STATUS_LOCK:
            return _status_payload(self._status,background_loop_enabled)

    def run(self,horizon=48,refresh=False):
        if isinstance(horizon,bool) or horizon not in (24,48) or not isinstance(horizon,int):
            raise ForecastError('Live horizon must be 24 or 48 hours')
        if not isinstance(refresh,bool):
            raise ForecastError('Live refresh must be boolean')
        with self._run_lock:
            try:
                return self._run(horizon,refresh)
            except Exception as exc:
                self._set_status(None,str(exc))
                if isinstance(exc,ForecastError):
                    raise
                raise ForecastError(f'Live forecast failed: {exc}') from exc

    def _run(self,horizon,refresh):
        for attempt in range(2):
            started=_timestamp(self.now(),'clock');origin=_ceil_hour(started)
            fid=self.store.start(_iso(origin),horizon)
            try:
                config,conversion,conversion_verified,conversion_assumed=self._configuration()
                meta,model_hash,trained,code_hash=self._model_metadata(started)
                config_hash=_hash_json(config)
                scenario=config.get('physics_scenario',{})
                scenario_enabled=scenario.get('enabled',False)
                combined_hash=_hash_json({'model':model_hash,'config':config_hash,'pipeline':PIPELINE_VERSION,'code':code_hash})
                self.store.event(fid,'live_acquire','started',{'forecast_start':origin.isoformat(),'refresh':refresh})
                frames={};currents={};provenances={}
                crossed=False
                for tid in (1,2):
                    if origin<_ceil_hour(self.now()):
                        crossed=True
                        break
                    payload=self.weather_fetch(tid,horizon,self.cache_dir,refresh=refresh,forecast_start=origin,now=self.now())
                    if origin<_ceil_hour(self.now()):
                        crossed=True
                        break
                    frame,current,prov=self._validate_weather(payload,origin,horizon,tid,_timestamp(self.now(),'clock'))
                    expected=config['turbines'][str(tid)];actual=prov.get('requested_coordinates',{})
                    if not isinstance(actual,dict) or any(not isinstance(actual.get(k),(int,float)) or abs(actual[k]-expected[k])>1e-6 for k in ('latitude','longitude')):
                        raise ForecastError(f'Weather coordinates do not match turbine {tid} configuration')
                    frames[tid]=frame;currents[tid]=current;provenances[tid]=prov
                checked=_timestamp(self.now(),'clock')
                if crossed or origin<_ceil_hour(checked):
                    self.store.fail(fid,'Acquisition crossed hour boundary; restarting')
                    self.store.event(fid,'live_restart','started',{'reason':'hour boundary'})
                    continue
                input_hash=_hash_json({str(t):{'rows':frames[t].assign(timestamp=frames[t].timestamp.map(lambda x:x.isoformat())).to_dict('records'),
                    'source_hash':provenances[t]['source_hash']} for t in (1,2)})
                self.store.event(fid,'live_acquire_validate','succeeded',{'input_hash':input_hash,'provenance':provenances})
                # Refresh reacquires weather. It does not imply changed inputs:
                # unchanged values and model can retain their original issue.
                cached=self.store.find_reusable(_iso(origin),horizon,input_hash,combined_hash)
                if cached and cached['result'].get('mode')=='live':
                    result=cached['result']
                    result.update(id=fid,reused=True,reused_from=cached['id'],checked_at=checked.isoformat())
                    for turbine in result['turbines']:
                        tid=turbine['turbine_id'];turbine.update(current=currents[tid],provenance=provenances[tid])
                else:
                    turbines=[]
                    warnings=['Current weather is a model estimate, not a turbine measurement.',
                              'The power model was fitted to measured weather; forecast wind height and site bias are uncalibrated.',
                              'Intervals omit weather forecast uncertainty. Current API does not provide the exact model initialization time.']
                    if not conversion:
                        warnings.append('MW/MWh conversion is disabled: turbine capacity and normalization formula are unverified.')
                    elif conversion_assumed:
                        warnings.append('MW/MWh values are estimates using the explicit rated-capacity-fraction assumption; the normalization formula is not verified against dataset documentation.')
                    if scenario_enabled:
                        warnings.append('Exploratory power-curve scenario is enabled. Thresholds are assumptions and baseline model metrics do not describe these adjusted outputs.')
                    age_days=max(0,int((checked-trained).total_seconds()//86400))
                    if age_days>180:
                        warnings.append(f'Model training data is {age_days} days old; current performance has not been validated.')
                    for tid in (1,2):
                        frame=frames[tid];pred=self.predict(self.artifact_dir,tid,frame).copy()
                        if {'timestamp','power','lower','upper'}-set(pred.columns) or len(pred)!=horizon:
                            raise ForecastError(f'Turbine {tid}: invalid model output')
                        if [_timestamp(v,'prediction timestamp') for v in pred.timestamp]!=list(frame.timestamp.dt.to_pydatetime()):
                            raise ForecastError('Model timestamps do not match live weather')
                        for col in ('power','lower','upper'):
                            pred[col]=pd.to_numeric(pred[col],errors='coerce')
                        if not pred[['power','lower','upper']].map(lambda x:math.isfinite(x) and 0<=x<=1).all().all() or (pred.lower>pred.power).any() or (pred.power>pred.upper).any():
                            raise ForecastError('Model returned invalid normalized power bounds')
                        pred=pred.reset_index(drop=True);pred['wind_speed']=frame.wind_speed.to_numpy()
                        raw=pred[['power','lower','upper']].copy();counts={}
                        for col in ('power','lower','upper'):
                            pred=_apply_physics_scenario(pred,col,scenario_enabled,
                                scenario.get('cut_in_speed',2.5),scenario.get('cut_out_speed',25.0))
                            counts[col]=int((raw[col]!=pred[col]).sum())
                        ranges=meta['turbines'][str(tid)].get('training_weather_ranges',{})
                        outside={key:int((~frame[key].between(*bounds)).sum()) for key,bounds in ranges.items() if key in frame and isinstance(bounds,list) and len(bounds)==2}
                        if any(outside.values()):
                            warnings.append(f'T{tid}: weather outside the training feature range: {outside}.')
                        cap=float(config['capacity_mw']) if conversion else None
                        points=[]
                        for i,row in pred.iterrows():
                            power=float(row.power);lower=float(row.lower);upper=float(row.upper)
                            points.append({'timestamp':frame.timestamp.iloc[i].isoformat(),'wind_speed':float(frame.wind_speed.iloc[i]),
                                'temperature':float(frame.temperature.iloc[i]),'normalized_power':power,'lower_normalized':lower,'upper_normalized':upper,
                                'raw_normalized_power':float(raw.power.iloc[i]),'raw_lower_normalized':float(raw.lower.iloc[i]),'raw_upper_normalized':float(raw.upper.iloc[i]),
                                'power_mw':power*cap if cap else None,'lower_mw':lower*cap if cap else None,'upper_mw':upper*cap if cap else None,
                                'energy_mwh':power*cap if cap else None})
                        turbines.append({'turbine_id':tid,'coordinates':config['turbines'][str(tid)],'current':currents[tid],
                            'provenance':provenances[tid],'points':points,'physics_corrections':sum(counts.values()),'physics_correction_counts':counts,'out_of_training_range':outside})
                    farm=[]
                    for a,b in zip(turbines[0]['points'],turbines[1]['points']):
                        farm.append({'timestamp':a['timestamp'],'normalized_power_mean_proxy':(a['normalized_power']+b['normalized_power'])/2,
                            **{k:a[k]+b[k] if conversion else None for k in ('power_mw','lower_mw','upper_mw','energy_mwh')}})
                    mean_proxy=sum(p['normalized_power_mean_proxy'] for p in farm)/horizon
                    result={'id':fid,'status':'succeeded','mode':'live','reused':False,'issued_at':checked.isoformat(),'checked_at':checked.isoformat(),
                        'forecast_start':origin.isoformat(),'forecast_end':(origin+timedelta(hours=horizon)).isoformat(),'horizon_hours':horizon,
                        'model':{'version':meta.get('model_version'),'trained_through':trained.isoformat(),'trained_at':trained.isoformat(),'age_days':age_days,'age_warning':age_days>180,'hash':model_hash,'availability_cutoff':meta['model_available_at']},
                        'capacity':{'verified':config.get('capacity_verified',False),'conversion_enabled':conversion,
                            'conversion_assumed':conversion_assumed,'conversion_verified':conversion_verified,
                            'configured_unverified_mw':None if config.get('capacity_verified',False) else config.get('capacity_mw'),
                            'per_turbine_mw':config.get('capacity_mw') if conversion else None,
                            'total_mw':config['capacity_mw']*2 if conversion else None,'source':config.get('capacity_source','configuration')},
                        'energy_conversion':{'enabled':conversion,'basis':config.get('power_normalization','unknown'),
                            'assumed':conversion_assumed,'verified':conversion_verified,
                            'source':config.get('power_normalization_evidence') if conversion else None},
                        'physics_scenario_enabled':scenario_enabled,'current_is_model_estimate':True,'initialization_time':None,'availability_basis':'actual_retrieval',
                        'turbines':turbines,'farm':{'points':farm,'peak_power_mw':max(p['power_mw'] for p in farm) if conversion else None,'total_energy_horizon_mwh':sum(p['energy_mwh'] for p in farm) if conversion else None},
                         'summary':{'normalized_mean_proxy':mean_proxy,'energy_24h_mwh':sum(p['energy_mwh'] for p in farm[:24]) if conversion else None,
                             'energy_horizon_mwh':sum(p['energy_mwh'] for p in farm) if conversion else None,
                             'power_unit':('MW_estimate' if conversion_assumed else 'MW') if conversion else 'normalized',
                             'energy_unit':(('MWh_estimate' if conversion_assumed else 'MWh') if conversion else None),
                             'energy_interval_hours':1},
                         'warnings':warnings,'limitations':warnings,'audit':{'input_hash':input_hash,'model_hash':model_hash,'configuration_hash':config_hash,'pipeline_version':PIPELINE_VERSION,'code_hash':code_hash,'weather_source_hashes':{str(t):provenances[t]['source_hash'] for t in (1,2)}}}
                finished=_timestamp(self.now(),'clock')
                if origin<_ceil_hour(finished):
                    self.store.fail(fid,'Inference crossed hour boundary; restarting')
                    self.store.event(fid,'live_restart','started',{'reason':'hour boundary'})
                    continue
                if self._model_metadata(finished)[1]!=model_hash or self._configuration()[0]!=config:
                    raise ForecastError('Model or configuration changed during live forecast; retry')
                result['checked_at']=finished.isoformat()
                if not result['reused']:
                    result['issued_at']=finished.isoformat()
                self.store.finish(fid,result,input_hash,combined_hash,stage='live_reuse' if result['reused'] else 'live_persist')
                self._set_status(result['issued_at'],None)
                return result
            except Exception as exc:
                self.store.fail(fid,str(exc));self.store.event(fid,'live_pipeline','failed',{'error':str(exc)})
                raise
        raise ForecastError('Live forecast repeatedly crossed an hour boundary; retry')


def _status_payload(status,background_loop_enabled):
    return {'status':'error' if status['error'] else ('ok' if status['last_successful_issue_at'] else 'pending'),
            **status,'background_loop_enabled':bool(background_loop_enabled)}


def live_status(background_loop_enabled=False):
    with _STATUS_LOCK:
        return _status_payload(_STATUS,background_loop_enabled)

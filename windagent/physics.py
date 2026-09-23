"""Optional illustrative power-curve scenario, never an implicit ML correction."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd

CUT_IN_SPEED = 2.5
RATED_SPEED = 10.5
CUT_OUT_SPEED = 25.0


def validate_thresholds(cut_in=CUT_IN_SPEED, rated=RATED_SPEED, cut_out=CUT_OUT_SPEED):
    values = (cut_in,rated,cut_out)
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in values):
        raise ValueError('Scenario thresholds must be finite numbers')
    if not 0 <= cut_in < rated < cut_out <= 100:
        raise ValueError('Scenario thresholds must satisfy 0 <= cut_in < rated < cut_out <= 100')
    return values


def apply_physics_sanity_check(df: pd.DataFrame, pred_col='predicted_power', wind_col='wind_speed',
                               *, enabled=False, cut_in=CUT_IN_SPEED, cut_out=CUT_OUT_SPEED) -> pd.DataFrame:
    """Return input unchanged by default; explicit scenario clips and cuts power.

    100 m grid wind is not known turbine hub wind; these thresholds are unverified.
    Enabling this changes predictions and invalidates baseline accuracy claims.
    """
    out=df.copy()
    if not enabled:
        return out
    if not 0 <= cut_in < cut_out <= 100:
        raise ValueError('Invalid cut-in/cut-out scenario thresholds')
    values=pd.to_numeric(out[pred_col],errors='raise')
    wind=pd.to_numeric(out[wind_col],errors='raise')
    if not np.isfinite(values).all() or not np.isfinite(wind).all() or (wind<0).any():
        raise ValueError('Scenario inputs must be finite with nonnegative wind')
    out[pred_col]=values.clip(0,1)
    out.loc[(wind<cut_in)|(wind>cut_out),pred_col]=0.0
    return out


def validate_physics_predictions(df, pred_col='power', wind_col='wind_speed', **kwargs):
    """Compatibility wrapper moved out of the historical orchestration module."""
    return apply_physics_sanity_check(df,pred_col,wind_col,**kwargs)


def calculate_physics_power_baseline(wind_speed: float, *, cut_in=CUT_IN_SPEED,
                                     rated=RATED_SPEED, cut_out=CUT_OUT_SPEED) -> float:
    """Illustrative bounded curve; not a measured turbine curve or trained model."""
    validate_thresholds(cut_in,rated,cut_out)
    if isinstance(wind_speed,bool) or not math.isfinite(float(wind_speed)) or wind_speed<0:
        raise ValueError('wind_speed must be finite and nonnegative')
    if wind_speed<cut_in or wind_speed>cut_out:
        return 0.0
    if wind_speed>=rated:
        return 1.0
    return float(np.clip(((wind_speed-cut_in)/(rated-cut_in))**2.2,0,1))

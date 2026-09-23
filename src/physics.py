import numpy as np
import pandas as pd

# Физические параметры турбин, определённые в ходе исследования
CUT_IN_SPEED = 2.5    # м/с (скорость включения генерации)
RATED_SPEED = 10.5    # м/с (скорость выхода на 100% мощности)
CUT_OUT_SPEED = 25.0  # м/с (скорость аварийного отключения)

def apply_physics_sanity_check(df: pd.DataFrame, pred_col: str = 'predicted_power', wind_col: str = 'wind_speed') -> pd.DataFrame:
    """
    Постобработка и проверка прогнозов ML-модели на соответствие физике ВЭС.
    """
    df = df.copy()
    
    # 1. Клиппинг мощности в физическом диапазоне [0.0, 1.0]
    df[pred_col] = df[pred_col].clip(lower=0.0, upper=1.0)
    
    # 2. Выработка равна 0 при ветре ниже Cut-in
    df.loc[df[wind_col] < CUT_IN_SPEED, pred_col] = 0.0
    
    # 3. Аварийное обнуление при штормовом ветре выше Cut-out
    df.loc[df[wind_col] > CUT_OUT_SPEED, pred_col] = 0.0
    
    return df

def calculate_physics_power_baseline(wind_speed: float) -> float:
    """
    Приближенный расчет физической мощности по кривой Power Curve.
    Используется как базовая физическая фича для ML-модели.
    """
    if wind_speed < CUT_IN_SPEED:
        return 0.0
    elif wind_speed >= RATED_SPEED and wind_speed <= CUT_OUT_SPEED:
        return 1.0
    elif wind_speed > CUT_OUT_SPEED:
        return 0.0
    else:
        # Аппроксимация выработки в рабочем диапазоне от 2.5 до 10.5 м/с
        progress = (wind_speed - CUT_IN_SPEED) / (RATED_SPEED - CUT_IN_SPEED)
        return float(np.clip(progress ** 2.2, 0.0, 1.0))

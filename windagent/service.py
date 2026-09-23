"""Shared application logic for the FastAPI and dependency-light HTTP servers."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .agent import ForecastAgent, ForecastError

PROJECT_HOME = Path(__file__).resolve().parents[1]


class ServiceError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@lru_cache(maxsize=1)
def _agent_for(home_text: str) -> ForecastAgent:
    return ForecastAgent(Path(home_text))


def get_agent() -> ForecastAgent:
    from .settings import load_env
    load_env(home() / ".env")
    return _agent_for(os.environ.get("WINDAGENT_HOME", str(PROJECT_HOME)))


def home() -> Path:
    return Path(os.environ.get("WINDAGENT_HOME", str(PROJECT_HOME)))


@lru_cache(maxsize=1)
def _live_agent_for(home_text: str):
    from .live import LiveForecastAgent
    return LiveForecastAgent(Path(home_text))


def get_live_agent():
    from .settings import load_env
    load_env(home() / ".env")
    return _live_agent_for(str(home()))


def live_poll_enabled() -> bool:
    from .settings import load_env
    load_env(home() / ".env")
    return os.environ.get("WINDAGENT_LIVE_POLL", "1").strip().lower() not in {"0", "false", "no", "off"}


def live_poll_loop(stop: threading.Event, interval: int = 300) -> None:
    """Refresh the actual live forecast periodically and log failures visibly."""
    logger = logging.getLogger("windagent.live_poll")
    while not stop.is_set():
        try:
            get_live_agent().run(48, refresh=True)
        except Exception:
            logger.exception("Background live forecast refresh failed")
        if stop.wait(interval):
            break


def fleet_summary(series: dict) -> dict:
    return {f"T{key}": {"mean": sum(row["power"] for row in rows) / len(rows),
                       "peak": max(row["power"] for row in rows),
                       "peak_at": max(rows, key=lambda row: row["power"])["timestamp"],
                       "hours": len(rows)} for key, rows in series.items()}


def horizon_value(value=48) -> int:
    if isinstance(value, bool):
        raise ServiceError("Горизонт должен быть 24 или 48 часов.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ServiceError("Горизонт должен быть 24 или 48 часов.") from exc
    if result not in (24, 48) or str(value) not in ("24", "48"):
        raise ServiceError("Горизонт должен быть 24 или 48 часов.")
    return result


def boolean_value(value=False) -> bool:
    if value in (True, "true", "1"):
        return True
    if value in (False, "false", "0"):
        return False
    raise ServiceError("refresh must be true or false")


def dashboard_forecast(turbine_id: str, as_of_date: str | None, horizon_hours=48,
                       refresh=False, *, agent=None, mode="backtest") -> dict:
    if turbine_id not in ("T1", "T2"):
        raise ServiceError("Выберите T1 или T2.")
    if mode not in ("backtest", "live"):
        raise ServiceError("mode must be backtest or live")
    if mode == "live":
        result = get_live_agent().run(horizon_value(horizon_hours), boolean_value(refresh))
        return live_dashboard(turbine_id, result)
    try:
        day = datetime.strptime(as_of_date, "%Y-%m-%d").date()
        if day.isoformat() != as_of_date:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ServiceError("as_of_date must use YYYY-MM-DD") from exc
    agent = agent or get_agent()
    timezone_name = agent._metadata().get("timezone", "Asia/Almaty")
    origin = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(timezone_name))
    horizon = horizon_value(horizon_hours)
    result = agent.run(origin.isoformat(), horizon=horizon, refresh=boolean_value(refresh))
    turbine = turbine_id[-1]
    # Use the exact covariates stored alongside this forecast, never a second fetch.
    weather_by_time = {row["timestamp"]: row for row in result["weather"][turbine]}
    forecast = []
    for row in result["turbines"][turbine]:
        weather = weather_by_time.get(row["timestamp"])
        if weather is None:
            raise ForecastError("Stored weather and forecast timestamps differ")
        forecast.append({"timestamp": row["timestamp"], "predicted_power": float(row["power"]),
                         "lower": float(row["lower"]), "upper": float(row["upper"]),
                         "wind_speed": float(weather["wind_speed"]), "temperature": float(weather["temperature"])})
    details = [
        {"severity": "info", "title": "Нормализованная мощность", "message": "Шкала 0–1 из CSV. Номинальная мощность и формула нормализации неизвестны: перевод в МВт и МВт·ч недоступен."},
        {"severity": "warning", "title": "Высота и время измерений", "message": "Погода: ветер на 100 м. Высота гондолы, часовой пояс исходных CSV и смысл метки интервала требуют подтверждения организаторов."},
        {"severity": "info", "title": "Условный интервал", "message": "Диапазон рассчитан по ошибкам модели на исторической погоде. Ошибка прогноза погоды в него не включена."},
    ]
    if not result.get("as_of_verified", False):
        details.insert(0, {"severity": "warning", "title": "Архив: доступность не подтверждена", "message": "Архив содержит ретроспективную реконструкцию. Время доступности выпуска принято с задержкой 12 ч; документального подтверждения публикации на as_of_date нет."})
    return {
        "turbine_id": turbine_id, "as_of_date": day.isoformat(), "generated_at": result["generated_at"],
        "horizon_hours": horizon, "forecast": forecast,
        "warnings": [item["message"] for item in details], "warning_details": details,
        "power_unit": "normalized", "capacity_mw": None, "origin": origin.isoformat(),
        "date_timezone": timezone_name, "model": result["model"], "provenance": result["provenance"].get(turbine, result["provenance"].get(int(turbine))),
        "forecast_id": result["id"], "audit_id": result["id"], "as_of_verified": result.get("as_of_verified", False),
        "mode": result.get("mode", "retrospective_reconstruction"),
        "fleet_summary": fleet_summary(result["turbines"]),
    }


def live_dashboard(turbine_id: str, result: dict) -> dict:
    """Project a single live run into the same UI contract as historical forecasts."""
    selected = next(item for item in result["turbines"] if str(item["turbine_id"]) == turbine_id[-1])
    energy_conversion = result.get("energy_conversion", {})
    conversion_enabled = bool(energy_conversion.get("enabled", False))
    forecast = [{"timestamp": row["timestamp"], "predicted_power": row["normalized_power"],
                 "lower": row["lower_normalized"], "upper": row["upper_normalized"],
                 "wind_speed": row["wind_speed"], "temperature": row["temperature"],
                 **({"power_mw": row.get("power_mw"), "lower_mw": row.get("lower_mw"),
                     "upper_mw": row.get("upper_mw"), "energy_mwh": row.get("energy_mwh")}
                    if conversion_enabled else {})}
                for row in selected["points"]]
    zone = "Asia/Almaty"
    origin = result["forecast_start"]
    day = datetime.fromisoformat(origin).astimezone(ZoneInfo(zone)).date().isoformat()
    raw_provenance = selected["provenance"]
    provenance = {**raw_provenance, "source": [raw_provenance.get("source", "Open-Meteo Forecast API")],
                  "initialized_at": [], "available_at": [], "retrieved_at": [raw_provenance["retrieved_at"]]}
    details = [
        {"severity": "info", "title": "Текущий прогноз", "message": "Погода получена сейчас. Этот режим не используется для исторического backtest за февраль."},
        {"severity": "info", "title": "Шкала мощности", "message": "Мощность показана в нормализованных единицах 0–1. Перевод в МВт требует подтверждения нормализации и номинальной мощности."},
        {"severity": "warning", "title": "Погодная неопределённость", "message": "Ветер на 100 м — приближение для площадки. Полоса графика не учитывает ошибку будущей погоды."},
    ]
    for warning in result.get("warnings", []):
        if isinstance(warning, str):
            if warning.startswith("Current weather is a model estimate") or warning.startswith("MW/MWh conversion"):
                continue
            if warning.startswith("Model training data is"):
                age = result.get("model", {}).get("age_days", "—")
                warning = f"С момента обучающих данных прошло {age} дней. Текущая точность модели отдельно не проверена."
            elif warning.startswith("Exploratory power-curve scenario"):
                warning = "Включён экспериментальный физический сценарий. Его пороги — допущения; опубликованные метрики исходной модели не описывают скорректированный выход."
            elif "outside the training feature range" in warning:
                warning = "Погодные признаки вышли за диапазон обучения: " + warning
            details.append({"severity": "warning", "title": "Проверка модели", "message": warning})
        elif isinstance(warning, dict):
            details.append({"severity": warning.get("severity", "warning"), "title": warning.get("title", "Проверка модели"), "message": warning.get("message", str(warning))})
    series = {str(t["turbine_id"]): [{"timestamp": r["timestamp"], "power": r["normalized_power"]} for r in t["points"]] for t in result["turbines"]}
    model = result.get("model", {})
    if "version" not in model:
        model = {**model, "version": model.get("model_version", model.get("name", "unknown"))}
    selected_farm = [{"timestamp": point["timestamp"],
                      **({"power_mw": point.get("power_mw"), "energy_mwh": point.get("energy_mwh")}
                         if conversion_enabled else {}),
                      "normalized_power_mean_proxy": point.get("normalized_power_mean_proxy")}
                     for point in result.get("farm", {}).get("points", [])]
    return {"turbine_id": turbine_id, "as_of_date": day, "generated_at": result.get("checked_at", result["issued_at"]),
            "horizon_hours": len(forecast), "forecast": forecast, "warnings": [w["message"] for w in details],
            "warning_details": details, "power_unit": "normalized",
            "capacity_mw": result.get("capacity", {}).get("per_turbine_mw"),
            "origin": origin, "date_timezone": zone, "model": model, "provenance": provenance,
            "forecast_id": result["id"], "audit_id": result["id"], "as_of_verified": False,
            "mode": "live", "current": selected.get("current"), "fleet_summary": fleet_summary(series),
            "capacity": result.get("capacity"), "energy_conversion": energy_conversion,
            "farm": {**result.get("farm", {}), "points": selected_farm}}


def status(*, agent=None) -> dict:
    from .chat import chat_configuration
    metadata = (agent or get_agent())._metadata()
    profile_path = home() / "reports/dataset_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else None
    return {"status": "ok", "model": metadata, "chat": chat_configuration(),
            "timezone": metadata.get("timezone", "Asia/Almaty"), "data_quality": profile,
            "strict_as_of": (agent or get_agent()).strict_as_of,
            "background_loop_enabled": live_poll_enabled(),
            "capabilities": {"live": True, "chat_tools": ["compare_turbines", "best_window", "daily_comparison", "ramp", "data_audit"]}}


def csv_content(record: dict | None) -> str:
    if record is None:
        raise ServiceError("Forecast not found", 404)
    if record.get("status") != "succeeded" or "result" not in record:
        raise ServiceError("CSV requires a successful forecast", 409)
    result = record["result"]
    if result.get("mode") == "live":
        buf = io.StringIO(newline="")
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "turbine_id", "normalized_power", "lower", "upper", "wind_speed", "temperature"])
        for turbine in result["turbines"]:
            for row in turbine["points"]:
                writer.writerow([row["timestamp"], turbine["turbine_id"], row["normalized_power"], row["lower_normalized"], row["upper_normalized"], row["wind_speed"], row["temperature"]])
        return buf.getvalue()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "turbine_id", "normalized_power", "lower", "upper", "farm_normalized_power_mean_proxy"])
    farm = {row["timestamp"]: row["normalized_power_mean_proxy"] for row in result["farm"]["series"]}
    for turbine_id, series in result["turbines"].items():
        for row in series:
            writer.writerow([row["timestamp"], turbine_id, row["power"], row["lower"], row["upper"], farm.get(row["timestamp"], "")])
    return buf.getvalue()

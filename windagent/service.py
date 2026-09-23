"""Shared application logic for the FastAPI and dependency-light HTTP servers."""
from __future__ import annotations

import csv
import io
import json
import os
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


def dashboard_forecast(turbine_id: str, as_of_date: str, horizon_hours=48,
                       refresh=False, *, agent=None) -> dict:
    if turbine_id not in ("T1", "T2"):
        raise ServiceError("Выберите T1 или T2.")
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
    }


def status(*, agent=None) -> dict:
    from .chat import chat_configuration
    metadata = (agent or get_agent())._metadata()
    profile_path = home() / "reports/dataset_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else None
    return {"status": "ok", "model": metadata, "chat": chat_configuration(),
            "timezone": metadata.get("timezone", "Asia/Almaty"), "data_quality": profile,
            "strict_as_of": (agent or get_agent()).strict_as_of}


def csv_content(record: dict | None) -> str:
    if record is None:
        raise ServiceError("Forecast not found", 404)
    if record.get("status") != "succeeded" or "result" not in record:
        raise ServiceError("CSV requires a successful forecast", 409)
    result = record["result"]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "turbine_id", "normalized_power", "lower", "upper", "farm_normalized_power_mean_proxy"])
    farm = {row["timestamp"]: row["normalized_power_mean_proxy"] for row in result["farm"]["series"]}
    for turbine_id, series in result["turbines"].items():
        for row in series:
            writer.writerow([row["timestamp"], turbine_id, row["power"], row["lower"], row["upper"], farm.get(row["timestamp"], "")])
    return buf.getvalue()

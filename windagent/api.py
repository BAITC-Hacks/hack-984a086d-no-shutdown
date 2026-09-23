"""HTTP API for forecast creation, inspection, export, and replay."""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent import ForecastAgent, ForecastError


PROJECT_HOME = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _agent_for(home_text: str) -> ForecastAgent:
    return ForecastAgent(Path(home_text))


def get_agent() -> ForecastAgent:
    return _agent_for(os.environ.get("WINDAGENT_HOME", str(PROJECT_HOME)))


app = FastAPI(title="Wind Forecast Agent", version="1.0.0")


class ForecastRequest(BaseModel):
    origin: str
    horizon: int = Field(default=48, ge=24, le=48)
    refresh: bool = False


class ReplayRequest(BaseModel):
    start: str = "2026-02-01T00:00:00+05:00"
    end: str = "2026-02-28T00:00:00+05:00"
    horizon: int = Field(default=48, ge=24, le=48)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "windagent", "home": os.environ.get("WINDAGENT_HOME", str(PROJECT_HOME))}


@app.get("/model")
def model_metadata() -> dict:
    try:
        return get_agent()._metadata()
    except ForecastError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/forecasts")
def create_forecast(request: ForecastRequest) -> dict:
    try:
        return get_agent().run(request.origin, request.horizon, request.refresh)
    except ForecastError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/forecasts")
def list_forecasts(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    return get_agent().store.list(limit)


@app.get("/forecasts/{forecast_id}")
def forecast_detail(forecast_id: int) -> dict:
    result = get_agent().store.get(forecast_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Forecast not found")
    return result


@app.get("/forecasts/{forecast_id}/csv")
def forecast_csv(forecast_id: int) -> StreamingResponse:
    record = get_agent().store.get(forecast_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Forecast not found")
    if record.get("status") != "succeeded" or "result" not in record:
        raise HTTPException(status_code=409, detail=f"Forecast is {record.get('status')}; CSV requires a successful forecast")
    result = record["result"]
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "turbine_id", "normalized_power", "lower", "upper", "farm_normalized_power_mean_proxy"])
    farm = {row["timestamp"]: row["normalized_power_mean_proxy"] for row in result["farm"]["series"]}
    for turbine_id in ("1", "2"):
        for row in result["turbines"][turbine_id]:
            ts = row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else str(row["timestamp"])
            writer.writerow([ts, turbine_id, row["power"], row["lower"], row["upper"], farm.get(ts, "")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="forecast-{forecast_id}.csv"'})


@app.post("/replay")
def replay(request: ReplayRequest) -> dict:
    try:
        return get_agent().replay(request.start, horizon=request.horizon, end=request.end)
    except ForecastError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/forecast")
def frontend_forecast(
    turbine_id: Literal["T1", "T2"],
    as_of_date: str,
    horizon_hours: int = Query(default=48, ge=24, le=48),
    refresh: bool = False,
) -> dict:
    """Compatibility view for the frontend prototype; values remain normalized."""
    try:
        try:
            day = datetime.strptime(as_of_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="as_of_date must use YYYY-MM-DD") from exc
        if day.isoformat() != as_of_date:
            raise HTTPException(status_code=422, detail="as_of_date must use YYYY-MM-DD")
        timezone_name = "Asia/Almaty"
        origin = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(timezone_name))
        agent = get_agent()
        turbine_number = 1 if turbine_id == "T1" else 2
        result = agent.run(origin.isoformat(), horizon=horizon_hours, refresh=refresh)
        # Reuse the just validated weather cache so this adapter includes covariates
        # without changing the original forecast result/API contract.
        weather = agent.weather_fetch(turbine_number, origin, horizon_hours, agent.cache_dir, refresh=False)
        weather, provenance = agent._validate_weather(weather, origin.astimezone(timezone.utc), horizon_hours, turbine_number)
        result_provenance = result["provenance"].get(str(turbine_number), result["provenance"].get(turbine_number))
        if provenance != result_provenance:
            raise ForecastError("Weather provenance changed while assembling the response; retry the forecast so predictions and inputs match")
        weather_by_time = {
            timestamp.isoformat(): {"wind_speed": float(wind), "temperature": float(temp)}
            for timestamp, wind, temp in zip(weather["timestamp"], weather["wind_speed"], weather["temperature"])
        }
        forecasts = []
        for row in result["turbines"][str(turbine_number)]:
            weather_row = weather_by_time.get(row["timestamp"])
            if weather_row is None:
                raise ForecastError("Validated weather timestamps do not match forecast timestamps")
            forecasts.append({"timestamp": row["timestamp"], "predicted_power": float(row["power"]), **weather_row})
        warning_details = [
            {"severity": "warning", "title": "Capacity unavailable", "message": "Power is normalized from 0 to 1; no MW or energy estimate is available because turbine capacity was not supplied."},
            {"severity": "info", "title": "Weather height assumption", "message": "Weather input is ECMWF 100 m wind speed; actual hub height and site bias are unverified."},
            {"severity": "info", "title": "Conditional uncertainty only", "message": "Prediction intervals reflect conditional historical residuals and exclude weather forecast uncertainty."},
        ]
        return {
            "turbine_id": turbine_id,
            "as_of_date": day.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "horizon_hours": horizon_hours,
            "forecast": forecasts,
            "warnings": [item["message"] for item in warning_details],
            "warning_details": warning_details,
            "power_unit": "normalized",
            "capacity_mw": None,
            "origin": origin.isoformat(),
            "date_timezone": timezone_name,
            "model": result["model"],
            "provenance": provenance,
            "forecast_id": result["id"],
            "audit_id": result["id"],
        }
    except ForecastError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Weather or forecast data unavailable: {exc}") from exc

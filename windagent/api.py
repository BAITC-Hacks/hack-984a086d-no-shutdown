"""HTTP API for forecast creation, inspection, export, and replay."""
from __future__ import annotations

import csv
import asyncio
import io
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agent import ForecastAgent, ForecastError
from .live import LiveForecastAgent, live_status


PROJECT_HOME = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _agent_for(home_text: str) -> ForecastAgent:
    return ForecastAgent(Path(home_text))


def get_agent() -> ForecastAgent:
    return _agent_for(os.environ.get("WINDAGENT_HOME", str(PROJECT_HOME)))


@lru_cache(maxsize=1)
def _live_agent_for(home_text: str) -> LiveForecastAgent:
    return LiveForecastAgent(Path(home_text))


def get_live_agent() -> LiveForecastAgent:
    return _live_agent_for(os.environ.get("WINDAGENT_HOME", str(PROJECT_HOME)))


_POLL_SECONDS = 300
_POLL_ENABLED = os.environ.get("WINDAGENT_LIVE_POLL", "1").strip().lower() not in {"0", "false", "no", "off"}


async def _live_poll_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(get_live_agent().run, 48, False)
        except Exception:
            # The live agent records the failure in its durable audit and status.
            pass
        await asyncio.sleep(_POLL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_live_poll_loop()) if _POLL_ENABLED else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Wind Forecast Agent", version="1.0.0", lifespan=lifespan)


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


@app.get("/api/live")
def live_forecast(hours: int = Query(default=48, ge=24, le=48), refresh: bool = False) -> dict:
    if hours not in (24, 48):
        raise HTTPException(status_code=422, detail="hours must be 24 or 48")
    try:
        return get_live_agent().run(hours, refresh)
    except ForecastError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Live weather or forecast data unavailable: {exc}") from exc


@app.get("/api/live/status")
def live_forecast_status() -> dict:
    return live_status(_POLL_ENABLED)


@app.get("/")
def dashboard_index() -> FileResponse:
    return FileResponse(PROJECT_HOME / "index.html", media_type="text/html")


@app.get("/app.js")
def dashboard_javascript() -> FileResponse:
    return FileResponse(PROJECT_HOME / "app.js", media_type="text/javascript")


@app.get("/styles.css")
def dashboard_stylesheet() -> FileResponse:
    return FileResponse(PROJECT_HOME / "styles.css", media_type="text/css")


@app.get("/assets/power_curves_turbines.png")
def power_curve_image() -> FileResponse:
    path = PROJECT_HOME / "assets" / "power_curves_turbines.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Power curve image is not installed")
    return FileResponse(path, media_type="image/png")


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
    if "summary" in result and "capacity" in result and "farm" in result and "points" in result["farm"]:
        writer.writerow(["timestamp", "turbine_id", "wind_speed_100m_mps", "temperature_c", "normalized_power", "power_mw", "lower_mw", "upper_mw", "energy_mwh"])
        for turbine in result["turbines"]:
            for row in turbine["points"]:
                writer.writerow([row["timestamp"], turbine["turbine_id"], row["wind_speed"], row["temperature"], row["normalized_power"], row["power_mw"], row["lower_mw"], row["upper_mw"], row["energy_mwh"]])
        buf.seek(0)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="live-forecast-{forecast_id}.csv"'})
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
            {"severity": "info", "title": "Normalized historical route", "message": "This legacy route returns normalized power with no MW or energy estimate. Use /api/live for configured MW and MWh estimates."},
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

"""FastAPI routes. The same service layer is used by the portable HTTP server."""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from . import service
from .agent import ForecastError
from .chat import answer

get_agent = service.get_agent


@asynccontextmanager
async def lifespan(_app):
    stop = threading.Event()
    worker = None
    if service.live_poll_enabled():
        worker = threading.Thread(target=service.live_poll_loop, args=(stop, 300),
                                  name="windagent-live-poll", daemon=True)
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker:
            await asyncio.to_thread(worker.join, 5)


app = FastAPI(title="Wind Forecast Agent", version="3.0.0", lifespan=lifespan)

class ForecastRequest(BaseModel):
    origin: str
    horizon: Literal[24, 48] = 48
    refresh: bool = False

class ReplayRequest(BaseModel):
    start: str = "2026-02-01T00:00:00+05:00"
    end: str = "2026-02-28T00:00:00+05:00"
    horizon: Literal[24, 48] = 48

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    turbine_id: Literal["T1", "T2"] = "T1"
    as_of_date: str = "2026-02-01"
    horizon_hours: Literal[24, 48] = 48
    history: list[dict] = Field(default_factory=list, max_length=30)
    mode: Literal["backtest", "live"] = "backtest"

def invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except service.ServiceError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except ForecastError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "Forecast service unavailable; see server log") from exc

@app.get("/health")
def health():
    return {"status": "ok", "service": "windagent"}

@app.get("/api/status")
def status():
    return invoke(service.status, agent=get_agent())

@app.get("/model")
def model_metadata():
    return invoke(get_agent()._metadata)

@app.post("/forecasts")
def create_forecast(request: ForecastRequest):
    return invoke(get_agent().run, request.origin, request.horizon, request.refresh)

@app.get("/forecasts")
def list_forecasts(limit: int = Query(default=100, ge=1, le=1000)):
    return get_agent().store.list(limit)

@app.get("/forecasts/{forecast_id}")
def forecast_detail(forecast_id: int):
    record = get_agent().store.get(forecast_id)
    if record is None:
        raise HTTPException(404, "Forecast not found")
    return record

@app.get("/forecasts/{forecast_id}/csv")
def forecast_csv(forecast_id: int):
    content = invoke(service.csv_content, get_agent().store.get(forecast_id))
    return Response(content, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="forecast-{forecast_id}.csv"'})

@app.post("/replay")
def replay(request: ReplayRequest):
    return invoke(get_agent().replay, request.start, horizon=request.horizon, end=request.end)

@app.get("/api/forecast")
def frontend_forecast(turbine_id: Literal["T1", "T2"], as_of_date: str | None = None,
                      horizon_hours: int = 48, refresh: bool = False, mode: Literal["backtest", "live"] = "backtest"):
    return invoke(service.dashboard_forecast, turbine_id, as_of_date, horizon_hours, refresh, agent=get_agent(), mode=mode)

@app.get("/live/forecast")
def live_forecast(horizon: Literal[24, 48] = 48, refresh: bool = False):
    return invoke(service.get_live_agent().run, horizon, refresh)

@app.get("/api/live")
def api_live(hours: Literal[24, 48] = 48, refresh: bool = False):
    """Compatibility endpoint returning the complete two-turbine live payload."""
    return invoke(service.get_live_agent().run, hours, refresh)

@app.get("/live/status")
def live_status():
    from .live import live_status as status
    return status()

@app.get("/api/live/status")
def api_live_status():
    return live_status()

@app.post("/api/chat")
def chat(request: ChatRequest):
    return invoke(answer, request.model_dump(), agent=get_agent())

@app.get("/")
def index():
    return FileResponse(service.PROJECT_HOME / "index.html")

@app.get("/styles.css")
def stylesheet():
    return FileResponse(service.PROJECT_HOME / "styles.css", media_type="text/css")

@app.get("/app.js")
def javascript():
    return FileResponse(service.PROJECT_HOME / "app.js", media_type="text/javascript")

@app.get("/assets/{name}")
def asset(name: str):
    if name not in {"wind-night.jpg", "energy-grid.jpg", "operator.jpg", "power_curves_turbines.png"}:
        raise HTTPException(404, "Not found")
    media_type = "image/png" if name.endswith(".png") else "image/jpeg"
    return FileResponse(service.PROJECT_HOME / "assets" / name, media_type=media_type)

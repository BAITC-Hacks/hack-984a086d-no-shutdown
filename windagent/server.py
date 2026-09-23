"""Portable localhost HTTP server; no FastAPI/uvicorn dependency required."""
from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import service
from .agent import ForecastError
from .chat import answer

STATIC = {"/": ("index.html", "text/html"), "/styles.css": ("styles.css", "text/css"), "/app.js": ("app.js", "text/javascript")}


class Handler(BaseHTTPRequestHandler):
    server_version = "WindAgent/2"

    def _send(self, value, status=200, content_type="application/json", headers=None):
        if content_type == "application/json":
            data = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        else:
            data = value if isinstance(value, bytes) else value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            if method == "GET" and path in STATIC:
                name, mime = STATIC[path]
                return self._send((service.PROJECT_HOME / name).read_bytes(), content_type=mime)
            payload = {}
            if method == "POST":
                # Local UI only. Reject foreign browser origins before taking any action.
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc != self.headers.get("Host"):
                    raise service.ServiceError("Cross-origin requests are disabled", 403)
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise service.ServiceError("Content-Type must be application/json", 415)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise service.ServiceError("JSON body must be between 1 and 65536 bytes", 413)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise service.ServiceError("JSON body must be an object")
            if path == "/health" and method == "GET":
                return self._send({"status": "ok", "service": "windagent"})
            if path == "/api/status" and method == "GET":
                return self._send(service.status())
            if path == "/api/forecast" and method == "GET":
                return self._send(service.dashboard_forecast(query.get("turbine_id"), query.get("as_of_date"), query.get("horizon_hours", 48), query.get("refresh", False)))
            if path == "/api/chat" and method == "POST":
                return self._send(answer(payload))
            if path == "/model" and method == "GET":
                return self._send(service.get_agent()._metadata())
            if path == "/forecasts" and method == "GET":
                limit = int(query.get("limit", 100))
                if not 1 <= limit <= 1000:
                    raise service.ServiceError("limit must be 1..1000")
                return self._send(service.get_agent().store.list(limit))
            if path == "/forecasts" and method == "POST":
                if not isinstance(payload.get("origin"), str):
                    raise service.ServiceError("origin is required")
                return self._send(service.get_agent().run(payload["origin"], service.horizon_value(payload.get("horizon", 48)), service.boolean_value(payload.get("refresh", False))))
            if path == "/replay" and method == "POST":
                return self._send(service.get_agent().replay(payload.get("start", "2026-02-01T00:00:00+05:00"), horizon=service.horizon_value(payload.get("horizon", 48)), end=payload.get("end", "2026-02-28T00:00:00+05:00")))
            match = re.fullmatch(r"/forecasts/(\d+)(/csv)?", path)
            if match and method == "GET":
                record = service.get_agent().store.get(int(match[1]))
                if match[2]:
                    content = service.csv_content(record)
                    return self._send(content, content_type="text/csv", headers={"Content-Disposition": f'attachment; filename="forecast-{match[1]}.csv"'})
                if record is None:
                    raise service.ServiceError("Forecast not found", 404)
                return self._send(record)
            raise service.ServiceError("Not found", 404)
        except service.ServiceError as exc:
            self._send({"detail": str(exc)}, exc.status)
        except (ForecastError, ValueError, TypeError) as exc:
            self._send({"detail": str(exc)}, 422)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("Request failed")
            self._send({"detail": "Forecast service unavailable; see server log"}, 502)


def serve(host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Wind Agent: http://{host}:{port} — Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

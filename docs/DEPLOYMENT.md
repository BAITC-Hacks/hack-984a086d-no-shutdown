# Deployment handoff

The user requested **repository only** and will choose hosting later. The demonstration server on the user's PC has been stopped. Nothing auto-starts on the PC, and this change does not publish a site or configure scheduled GitHub forecasting jobs.

The application has a Python backend as well as HTML/CSS/JavaScript. Deploy them together so `/api/live` and the dashboard share one origin. The existing `Dockerfile` packages Python 3.12, dependencies, trained models, configuration and dashboard. It listens on port 8000. The team's chosen container host must permit outbound HTTPS requests to `api.open-meteo.com` and writable directories for `state/` and `data/live_weather/`.

Example commands for the team's deployment environment:

```sh
docker build -t wind-agent .
docker run --rm -p 8000:8000 -v wind-state:/app/state -v wind-weather:/app/data/live_weather wind-agent
```

Use one application worker for the current design: each worker starts its own five-minute polling loop. `WINDAGENT_LIVE_POLL=0` disables that loop if an external worker handles updates. `/health` confirms the HTTP service is alive; `/api/live/status` reports whether a forecast has succeeded and the latest error. Configure TLS and access control at the chosen host before external deployment.

The API computes a forecast on request and also checks weather every five minutes while the server is active. It needs no weather API key for the documented free Open-Meteo endpoint. If the team later separates static hosting and the backend, update the frontend URL configuration and access policy together.

GitHub holds the source and test workflow. A repository checkout alone does not run the Python API or generate new forecasts. The saved September forecast is evidence of a successful acquisition, not a continuously updated deployment. No GitHub Pages URL or hosted uptime is claimed.

Before deployment:

1. Run `python -m pip install -r requirements.lock.txt` and `python -m pytest -q`.
2. Run `python -m windagent live --horizon 48 --refresh` to test weather access on the selected host.
3. Verify the dashboard, `/api/live/status`, weather retrieval timestamps, and 2.5 MW capacity configuration.

For a reproducible audit of the saved delivery evidence, run `python scripts/verify_live_delivery.py`. The Docker image was prepared in the repository but was not built or deployed during this task.

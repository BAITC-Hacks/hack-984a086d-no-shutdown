# Live forecasting

## Run

The user selected repository-only delivery and will deploy later. The local demonstration server was stopped. No scheduled PC task or hosted deployment was created.

Run `start.ps1` in PowerShell, then open http://127.0.0.1:8000/. The server acquires weather at startup and every 300 seconds; the browser refreshes on the same cadence. The service runs locally while the process and PC remain active. It is not a remotely hosted or reboot-persistent deployment.

`python -m windagent live --horizon 48 --refresh` saves a real acquisition and forecast to `reports/live_forecast.json` and `reports/live_forecast.csv`. These reports are timestamped snapshots; they are not continuously updated unless the command or `watch` loop is running. `python -m windagent watch --interval 300` updates the JSON report repeatedly. Historical forecasts and replay commands remain available separately.

## Weather and coordinates

The [Open-Meteo Forecast API](https://open-meteo.com/en/docs) is queried with `models=ecmwf_ifs`, UTC, wind in m/s, temperature in Celsius, 100 m wind and 2 m temperature. Both current conditions and forecasts are weather-model estimates; no SCADA or turbine availability feed is connected. Forecast hours start at the next full UTC hour, so the current partial hour is excluded from energy totals.

| Turbine | Latitude | Longitude | Rated MW |
|---|---:|---:|---:|
| 1 | 43.645150 | 78.535604 | 2.5 |
| 2 | 43.643198 | 78.538828 | 2.5 |

Coordinates come from the user's map links. Rated power was supplied by the user and interpreted as 2.5 MW for each turbine. Coordinates in the weather provider and configuration must agree. The turbines are close enough that the weather grid can return identical weather for both; their independently trained generation models can still differ.

The live API does not disclose a weather-model initialization timestamp. This field stays null rather than being inferred from retrieval time. Raw response content, source hashes, returned grid coordinates and retrieval timestamps accompany each forecast. A cache is accepted for at most 300 seconds; failed refreshes never silently substitute old weather.

## Model and physics

The supplied telemetry ends on January 31, 2026. Existing trained histogram gradient boosting models are used with their original feature schema. The API and dashboard expose training age and warn when it exceeds 180 days. Fresh weather does not make the training data fresh; updating the model needs new measured generation data and validation.

The teammate's `src/physics.py` from GitHub main is called through a validation helper in `windagent/agent.py`. Live point predictions and interval bounds obey its 0–1 limits, 2.5 m/s cut-in, and cut-out above 25 m/s. These thresholds are the research module's assumptions, not independently verified equipment specifications. Raw predictions and correction counts are audited. `calculate_physics_power_baseline` is available but was not inserted into the existing model's feature vector, which would require retraining and comparison.

The power-curve image in `assets/power_curves_turbines.png` comes from `Medina150207-feature/physics` commit `2c012428ff9077483c7109484efa38c6455cfbe2`; it is shown on the dashboard with its original normalized scale. The source physics module came from main commit `0fe607f453a8c804733f9c9b4dcf81b3295b9534`. The FRONT branch contained only an older README, so the live UI builds on the project's existing dashboard.

Each hourly normalized prediction is multiplied by 2.5 MW. Energy for that one-hour interval is the same numeric value in MWh. Farm values sum both turbines. Uncertainty bands describe historical conditional model residuals, exclude full weather forecast uncertainty, and are not a guarantee of generation. Historical replay artifacts retain their original normalized units.

## API and audit

- `GET /api/live?hours=48&refresh=false`: current 24/48-hour forecast with MW/MWh, timestamps and provenance.
- `GET /api/live/status`: last successful forecast issue, most recent check and polling status.
- `GET /docs`: interactive endpoint documentation, including historical forecasts.

Live runs and events are stored in `state/windagent.sqlite3`. Reuse requires matching weather, model, configuration, physics and pipeline hashes and preserves the original issue time. Working source snapshots live in `data/live_weather` and are excluded from Git. The checked delivery snapshot and its immutable source evidence are `reports/live_forecast.json`, `reports/live_forecast.csv`, and `reports/live_weather_turbine_*.json`. Browser errors retain the last successful display only with an explicit stale label. The service serves only the dashboard's named assets, not the raw data directory.

Validate offline with `python -m pytest -q`. Tests exercise cache expiry, source validation, forecast bounds, energy sums, API behavior and polling. Actual network evidence is saved separately in the live forecast report and weather snapshots.

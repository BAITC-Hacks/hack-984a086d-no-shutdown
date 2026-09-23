# Implementation plan and integration contract

Build an autonomous wind generation forecasting backend for the HackAlem brief. Forecast normalized hourly turbine power for 24–48 hours using dated weather model runs, preserving all as-of boundaries. No capacity is supplied, so do not label normalized power as kW or energy as kWh.

## Evidence and assumptions
- Turbine 1: 43.645150, 78.535604; turbine 2: 43.643198, 78.538828, resolved from the supplied Google Maps links.
- Both source CSVs end at 2026-01-31 23:50. February ground truth is absent.
- Input timestamps have no timezone. Assume Asia/Almaty (site local time), expose configuration and record this assumption. Ten-minute rows represent interval starts; an hourly mean becomes available at the end of that hour.
- Preserve gaps; no future interpolation. Aggregate hourly only with adequate coverage (at least 4 distinct ten-minute samples). Record dropped/invalid records.
- Training excludes February. Validate chronologically in pre-February data. Weather-to-power validation on observed wind is a conditional diagnostic, not proof of day-ahead accuracy.
- Weather must use actual individual initialized forecast runs, never reanalysis or stitched historical weather as a replay substitute. Use ECMWF IFS HRES 9km via Open-Meteo Single Runs, documented available from March 2024. Conservatively allow 12 hours after initialization for publication; this is an assumption, not a measured publication timestamp.
- Freeze production model at 2026-02-01T00:00:00+05:00; replay origins at local 00:00 February 1–28, with one optional January 31 run requiring a separate earlier training cutoff. First origin corresponds to end of January 31.

## Package and contracts
Python package `windagent`, project root is this file's parent parent. Paths passed explicitly; no user-machine hardcoded paths in production code. Main interpreter during development: workspace `work/venv/Scripts/python.exe` (outside project).

### Model agent owns windagent/data.py, windagent/model.py, windagent/train.py, tests/test_model.py
- `load_hourly(data_dir: Path, timezone: str = 'Asia/Almaty') -> pandas.DataFrame`, columns `turbine_id` (1/2 integer), `timestamp` (UTC-aware interval start), `wind_speed`, `temperature`, `power`, `sample_count`. data_dir contains turbine_1.csv and turbine_2.csv.
- `train_models(data_dir: Path, artifact_dir: Path, cutoff: str = '2026-02-01T00:00:00+05:00', timezone: str = 'Asia/Almaty') -> dict` trains deterministic candidate models + baseline, time split, saves metadata.json and per-turbine artifacts; records model availability cutoff, input hashes, feature schema, metrics and limitations. All training hours must be complete before cutoff.
- `predict_power(artifact_dir: Path, turbine_id: int, weather: pandas.DataFrame) -> pandas.DataFrame`, weather columns timestamp (UTC-aware), wind_speed, temperature; output same rows plus power, lower, upper. Bounds are pretest calibrated conditional residual intervals, explicitly not weather forecast uncertainty. Safe local artifact loading only.
- Use sklearn boosted trees or other suitable efficient model and a wind-bin curve baseline. Holdout candidate selection must not contaminate final diagnostic holdout. Train actual data and save meaningful reports.

### Weather agent owns windagent/weather.py, tests/test_weather.py, docs/WEATHER.md
- `fetch_weather(turbine_id: int, origin: str|Timestamp, horizon: int, cache_dir: Path, refresh: bool = False) -> pandas.DataFrame` returns EXACT horizon hours from origin inclusive, UTC-aware timestamp, wind_speed m/s, temperature C, plus initialized_at, available_at, source, source_hash. Reject naive origin, invalid horizon, missing hours, null/nonfinite values, availability after origin. Cache exact raw response+provenance; tolerate retryable errors with bounded retries; no silently fabricated weather. Separate content hash from fetch time; refresh supports detection of updated inputs.
- `TURBINES` coordinates constant. Use explicit ECMWF model run <= origin minus 12h; choose initialized 00/06/12/18 UTC at or before threshold; request enough hours for horizon plus lead. Document single runs endpoint, model name, height assumption and publication lag.
- Test with fixtures mocked offline. Try actual dated requests and save successful January/February cached runs to data/weather. Do not modify files owned by others.

### Backend agent owns windagent/api.py, windagent/agent.py, windagent/storage.py, tests/test_api.py, tests/test_agent.py
- FastAPI application `windagent.api:app` with health, model metadata, forecast creation/list/detail/CSV and replay endpoints. Paths via WINDAGENT_HOME, default project root. No network on import.
- Agent orchestrates acquire -> validate -> infer -> analyze -> persist, makes decisions to reuse unchanged inputs or recompute changed inputs/model, bounded retry handled weather layer; durable SQLite audit/state, no false success on failure. Must reject forecast origins preceding model cutoff. Use contracts above.
- `ForecastAgent(home: Path).run(origin: str, horizon: int = 48, refresh: bool = False) -> dict`, calls both turbines and returns id/status/forecasts/analysis/provenance. Tests inject/mock network and model. Equal-weight farm normalized mean explicitly a proxy; no fabricated capacity.
- Replay origins Feb 1–28 local 00:00, records failures transparently; reports February metrics unavailable absent actuals. Expose CLI interface for supervisor to wire. Autonomous loop callable watches input hashes and new origin; deterministic policy agent is core, do not claim LLM use.

## Supervisor owns integration, docs, packaging, CLI, end-to-end checks and GitHub commit
Will independently inspect all code, verify leakage guards and hashes, run tests and real model inference/replay, produce requirements lock, commands and results. Preserve raw inputs locally and in the private project repository if practical. Commit code, tests, reports, reproducibility metadata and trained artifacts. No credentials or transient venv/cache files in git.

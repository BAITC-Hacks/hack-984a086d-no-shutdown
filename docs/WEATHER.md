# Weather provenance: operational forecasts vs reconstruction

## What the supplied files establish

The 70 full-horizon JSON files (plus a 24-hour cache) under `data/weather/` contain individual `ecmwf_ifs` runs
requested for two coordinates and stored in September 2026. They preserve the
request, returned hourly weather, semantic hash, selected run initialization and
an **assumed** availability timestamp. They cover January 1–7 and February 1–28
forecast origins at 00:00 +05:00. These files are useful for a reproducible
reconstruction. They do **not** prove that the exact weather values were published
and available at each historical origin. A matching hash checks consistency of
local data, not the authenticity or historical publication time of a provider.

## Provider documentation checked on 2026-09-23

[Open-Meteo Single Runs API](https://open-meteo.com/en/docs/single-runs-api)
documents ECMWF IFS from March 2024 and calls the earlier archive Cycle 49R1
hindcasts. Other models are generally archived there from April 2, 2026. The `run`
parameter is initialization time, not publication time. Documentation of historical
coverage therefore does not establish operational availability in February.

[Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api)
combines successive runs into a time series. It cannot replace an individual
48-hour forecast issued at a historical origin. This project does not query that
endpoint or substitute reanalysis when a run is missing.

The supplied JSON metadata contains no contemporaneous publication receipt. This
is an unresolved requirement for an official competition backtest; no claim of
verified absence of future information should be made for this weather archive.
A real operational archive or written provider clarification about these exact
runs, their production inputs and dissemination times is needed before changing
this status. This review did not make a new live weather acquisition.

## Modes and fields

The default mode is `historical_reconstruction`, allowing the supplied cache to
be displayed with visible warnings. Every built-in weather result has:

```json
{
  "availability_basis": "assumed_12h_lag",
  "provenance_status": "unverified_hindcast",
  "as_of_verified": false
}
```

`available_at` is initialization + 12 hours. The client selects the latest
00/06/12/18 UTC run satisfying that rule and requires all requested valid times.
This is an explicit simulation policy, never an observed publication timestamp.
The origin is the beginning of the first predicted hour; all stored timestamps
are UTC-aware. Production model training must end before that first hour.

Set `WINDAGENT_STRICT_AS_OF=1` before starting the server, or use
`ForecastAgent(home, strict_as_of=True)`, to refuse unverified historical weather.
The built-in archive will fail with an actionable message. The lower-level
`fetch_weather(..., strict_as_of=True)` behaves the same way. Turning strict mode
off does not certify the results. Imported cache fields cannot self-certify a
run: status is assigned by the adapter policy on every read.

An alternative verified adapter must return a boolean `as_of_verified`, a
`publication_evidence` reference, `initialized_at <= available_at <= origin`, and
one consistent run per turbine. Evidence still requires human/provider review;
field validation is not a substitute for reviewing its provenance.

## Acquisition and cache validation

Coordinates are T1 `(43.645150, 78.535604)` and T2 `(43.643198, 78.538828)`.
The request uses `wind_speed_100m` in m/s and `temperature_2m` in °C. Actual turbine
hub height is unknown, and measured training wind may be from a different height.
The provider grid coordinates can differ from the turbine coordinates; both
nearby turbines may receive the same coarse grid cell. The 100 m wind choice is
a proxy and is not bias calibrated to the site.

Every cache read validates request identity, expected run, coordinates, units,
timezone, consecutive hours, finite values, nonnegative wind and semantic hash.
24-hour requests reuse the first half of a fully validated 48-hour cache offline.
An invalid cache is not returned; acquisition is attempted instead. Temporary
provider failures are retried with bounded backoff. Missing hours are an error,
not interpolated observations or generated mocks. JSON writes use temporary files
and atomic rename. Raw response hashes are stored but the original byte stream
is not, so only the semantic hash can be recomputed from the delivered JSON.

## Agent integrity and refresh

Each persisted result includes the exact covariates used by inference. The API
can show those values without fetching different weather after prediction.
The pipeline version is part of its cache identity, preventing reuse of earlier
results that omitted these warnings. Changed weather or model hashes trigger
recalculation; unchanged inputs reuse a previous calculation and create a new
audit entry. A refresh polls the same historical model run, so a successful
refresh does not imply that a newer run existed at the historical origin.
Failures are persisted and reported. A model that changes while a forecast is
running is rejected to avoid mixing models or reporting the wrong artifact hash.

Training interval endpoints are checked against the declared model cutoff and
forecast origin. These guards prevent known local leakage. They cannot establish
provider-side availability, CSV timezone conventions, or unknown normalization.

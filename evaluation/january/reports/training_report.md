# Wind power model training report

Model version: `conditional-power-v1`. Frozen availability cutoff: `2026-01-01T00:00:00+05:00` (2025-12-31T19:00:00Z UTC).

## Chronological diagnostics

Candidates were selected using a 65%/15% chronological fit/validation split. The final 20% was held out from selection; diagnostic models were fitted only on earlier observations. Production artifacts were then refit on all eligible hours before the cutoff.

| Turbine | Eligible hours | Selected model | Validation MAE | Holdout MAE | Holdout RMSE | Holdout R² |
|---:|---:|---|---:|---:|---:|---:|
| 1 | 22,983 | hist_gradient_boosting | 0.0315 | 0.0252 | 0.0540 | 0.975 |
| 2 | 24,174 | hist_gradient_boosting | 0.0255 | 0.0208 | 0.0489 | 0.979 |

These scores use observed wind speed and temperature. They measure the conditional power curve/model only, not 24–48-hour forecast accuracy. February actuals are unavailable.

## Data quality

See `dataset_profile.json` for raw-row counts, invalid rows, duplicates, undercovered hours, gaps, value ranges, and source hashes.

## Interval meaning

Lower and upper bounds use the 90th percentile absolute residual from the pre-holdout validation block, applied symmetrically and clipped to normalized power [0, 1]. They are conditional on the weather features supplied to the model and do not include uncertainty in the weather forecast.

## Reproducibility

Training uses deterministic scikit-learn estimators with fixed random state, per-source SHA-256 hashes, and a separate artifact for each turbine. The input data ends at 2026-01-31 23:50 local time; complete hourly means are required before the cutoff.

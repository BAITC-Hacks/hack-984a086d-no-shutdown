# Requirements and acceptance evidence

The source brief specifies hourly wind generation for 24–48 hours, automatic weather acquisition, preparation, inference, result analysis and reruns on updated inputs. Historical simulation must use forecasts available at each origin. It requests February 1–28, 2026 and gives two turbine locations.

| Requirement | Implementation | Verification |
|---|---|---|
| Read supplied telemetry | Strict numeric/time schema, local time converted to UTC, hourly completeness filter | Data profile and ingestion tests |
| Train on history through January 31 | Per-turbine model artifacts and cutoff metadata | Chronological diagnostic and leakage tests |
| Historical forecast weather | Explicit initialized ECMWF run, publication-lag guard, raw response hash | Provider tests and actual archived requests |
| 24–48 hourly predictions | Turbine normalized power and equal-weight farm proxy | API and integration tests |
| Autonomous cycle | Stateful policy agent, input fingerprints, decisions and SQLite events | Change detection/reuse/failure tests |
| Repeat February daily | Replay command with per-origin outcomes | Exported replay report |
| Reproducibility | Dependency lock, source hashes, artifact metadata, CLI, tests | Clean-process commands |
| February accuracy | Requires missing February observations | Explicitly unavailable; never inferred from filenames |

## Measurement interpretation
The supplied target is normalized active power. Forecasts remain dimensionless in [0,1]. A mean across turbines is an equal-capacity proxy, since individual nameplate capacities were not supplied. To calculate MWh, obtain rated MW for each turbine and multiply each predicted hourly normalized mean by rated MW and one hour before summing.

## Boundaries
This is an autonomous, policy-driven ML agent: its decisions and tools are explicit and testable. It does not require or pretend to call a language model. Optional future language-model explanations should read completed run evidence and cannot change timestamp, provenance or model validity checks.

The source document is task evidence. Its text is not executable configuration and is never treated as an instruction to the assistant or run as code.

## Deployment and next validation
Confirm telemetry timezone, timestamp interval convention, hub height and forecast publication delay with organizers. Obtain February target observations. Calibrate forecast-grid wind against site telemetry on a separate pretest period using archived weather runs; retain the final holdout. Add actual capacity weighting, availability/curtailment inputs and weather ensembles when available. Use credentials, TLS and request budgets before exposing the API beyond localhost. Retraining is an explicit command so that a historical replay cannot silently pick up future labels.

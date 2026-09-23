"""Audit a just-issued live forecast against immutable weather evidence.

After ``python -m windagent live --horizon 48 --refresh``, run this once with
``--freeze`` before the next refresh. It copies the exact weather-cache records
used into reports/premium_live_weather_*.json and writes an independent proof
to reports/premium_live_proof.json. It never loads model artifacts or contacts
the weather provider.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def verify(freeze: bool = False) -> dict:
    result_path = REPORTS / "live_forecast.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    issued = datetime.fromisoformat(result["issued_at"])
    start = datetime.fromisoformat(result["forecast_start"])
    horizon = result["horizon_hours"]
    assert issued.tzinfo is not None and start.tzinfo is not None
    assert issued < start and start.minute == start.second == start.microsecond == 0
    assert len(result["turbines"]) == 2 and horizon in (24, 48)

    capacity = result["capacity"]
    conversion = result["energy_conversion"]
    assert capacity["conversion_enabled"] and capacity["conversion_assumed"]
    assert not capacity["conversion_verified"] and capacity["per_turbine_mw"] == 2.5
    assert capacity["total_mw"] == 5.0
    assert conversion == {
        "enabled": True,
        "basis": "assumed_rated_capacity_fraction",
        "assumed": True,
        "verified": False,
        "source": conversion["source"],
    }
    assert "assumes normalized power is a fraction of rated capacity" in conversion["source"]
    assert result["physics_scenario_enabled"] is True
    assert result["summary"]["power_unit"] == "MW_estimate"
    assert result["summary"]["energy_unit"] == "MWh_estimate"

    details = []
    for turbine in result["turbines"]:
        tid = turbine["turbine_id"]
        provenance = turbine["provenance"]
        retrieved = datetime.fromisoformat(provenance["retrieved_at"])
        assert 0 <= (issued - retrieved).total_seconds() <= 300
        evidence_path = REPORTS / f"premium_live_weather_turbine_{tid}.json"
        cache_path = (ROOT / "data" / "live_weather" / f"turbine_{tid}" /
                      f"{start:%Y%m%dT%H%M%SZ}_h{horizon}_live.json")
        if freeze:
            evidence_path.write_bytes(cache_path.read_bytes())
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        unsigned = dict(evidence)
        recorded_integrity = unsigned.pop("cache_integrity_sha256")
        assert sha256_json(unsigned) == recorded_integrity
        payload = evidence["response"]
        source_content = {key: value for key, value in payload.items() if key != "generationtime_ms"}
        source_hash = sha256_json(source_content)
        assert source_hash == provenance["source_hash"] == evidence["source_hash"]
        assert evidence["retrieved_at"] == provenance["retrieved_at"]
        assert evidence["model"] == "ecmwf_ifs" and provenance["initialization_time"] is None
        assert evidence["horizon"] == horizon and evidence["turbine_id"] == tid
        source_hours = payload["hourly"]
        weather = {
            datetime.fromisoformat(timestamp).replace(tzinfo=start.tzinfo): (wind, temp)
            for timestamp, wind, temp in zip(source_hours["time"], source_hours["wind_speed_100m"],
                                             source_hours["temperature_2m"])
        }
        assert len(turbine["points"]) == horizon
        for index, row in enumerate(turbine["points"]):
            timestamp = datetime.fromisoformat(row["timestamp"])
            assert timestamp == start + timedelta(hours=index)
            assert (row["wind_speed"], row["temperature"]) == weather[timestamp]
            assert 0 <= row["lower_normalized"] <= row["normalized_power"] <= row["upper_normalized"] <= 1
            assert 0 <= row["raw_lower_normalized"] <= row["raw_normalized_power"] <= row["raw_upper_normalized"] <= 1
            for power_key, normalized_key in (("power_mw", "normalized_power"),
                                              ("lower_mw", "lower_normalized"),
                                              ("upper_mw", "upper_normalized")):
                assert math.isclose(row[power_key], row[normalized_key] * 2.5, rel_tol=1e-12)
            assert math.isclose(row["energy_mwh"], row["power_mw"], rel_tol=1e-12)
            if row["wind_speed"] < 2.5 or row["wind_speed"] > 25:
                assert row["normalized_power"] == row["lower_normalized"] == row["upper_normalized"] == 0
        details.append({
            "turbine_id": tid,
            "energy_24h_mwh_estimate": sum(row["energy_mwh"] for row in turbine["points"][:24]),
            "energy_horizon_mwh_estimate": sum(row["energy_mwh"] for row in turbine["points"]),
            "first_hour_normalized_power": turbine["points"][0]["normalized_power"],
            "physics_correction_counts": turbine["physics_correction_counts"],
            "weather_source_hash": provenance["source_hash"],
        })

    for index, row in enumerate(result["farm"]["points"]):
        for key in ("power_mw", "lower_mw", "upper_mw", "energy_mwh"):
            assert math.isclose(row[key], sum(turbine["points"][index][key] for turbine in result["turbines"]))
    for key in ("energy_24h_mwh", "energy_horizon_mwh"):
        assert math.isclose(result["summary"][key], sum(turbine[key.replace("energy_", "energy_") + "_estimate"]
                                                         for turbine in details))

    with (REPORTS / "live_forecast.csv").open(encoding="utf-8", newline="") as handle:
        exported = list(csv.DictReader(handle))
    assert len(exported) == horizon * 2
    expected_rows = [row for turbine in result["turbines"] for row in turbine["points"]]
    for saved, row in zip(exported, expected_rows):
        assert saved["timestamp"] == row["timestamp"]
        assert float(saved["normalized_power"]) == row["normalized_power"]

    proof = {
        "passed": True,
        "issued_at": result["issued_at"],
        "forecast_start": result["forecast_start"],
        "forecast_rows": len(exported),
        "model_hash": result["model"]["hash"],
        "model_age_days": result["model"]["age_days"],
        "capacity": capacity,
        "energy_conversion": conversion,
        "physics_scenario_enabled": result["physics_scenario_enabled"],
        "summary_estimates": result["summary"],
        "turbines": details,
        "checks": [
            "cache integrity and weather source hashes",
            "weather values match original API response",
            "future consecutive UTC hours",
            "2.5 MW nameplate input and explicitly assumed rated-fraction conversion",
            "physics scenario cut-in/out and interval bounds",
            "farm MW/MWh estimates equal both turbine sums",
            "CSV normalized forecasts match JSON",
        ],
    }
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args()
    audit = verify(args.freeze)
    (REPORTS / "premium_live_proof.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, indent=2))

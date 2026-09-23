"""Independently verify a saved live forecast and freeze its weather evidence.

Run immediately after `python -m windagent live --refresh`, before another live
acquisition can overwrite the working cache. Repeat later without --freeze to
audit the immutable evidence rather than the expiring cache.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(freeze: bool = False) -> dict:
    result = json.loads((ROOT / "reports/live_forecast.json").read_text(encoding="utf-8"))
    issued = datetime.fromisoformat(result["issued_at"])
    start = datetime.fromisoformat(result["forecast_start"])
    horizon = result["horizon_hours"]
    assert issued < start and start.minute == start.second == 0
    assert len(result["turbines"]) == 2 and horizon in (24, 48)
    capacity = result["capacity"]["per_turbine_mw"]
    assert capacity == 2.5 and result["capacity"]["total_mw"] == 5
    details = []
    for turbine in result["turbines"]:
        tid = turbine["turbine_id"]
        provenance = turbine["provenance"]
        retrieved = datetime.fromisoformat(provenance["retrieved_at"])
        assert 0 <= (issued - retrieved).total_seconds() <= 300
        evidence_path = ROOT / f"reports/live_weather_turbine_{tid}.json"
        if freeze:
            cache_path = ROOT / "data/live_weather" / f"turbine_{tid}" / f"{start:%Y%m%dT%H%M%SZ}_h{horizon}_live.json"
            evidence_path.write_bytes(cache_path.read_bytes())
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        canonical = json.dumps(evidence["response"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert hashlib.sha256(canonical.encode()).hexdigest() == provenance["source_hash"] == evidence["source_hash"]
        assert evidence["retrieved_at"] == provenance["retrieved_at"]
        assert evidence["model"] == "ecmwf_ifs" and provenance["initialization_time"] is None
        source_hours = evidence["response"]["hourly"]
        weather = {datetime.fromisoformat(t).replace(tzinfo=start.tzinfo): (w, temp)
                   for t, w, temp in zip(source_hours["time"], source_hours["wind_speed_100m"], source_hours["temperature_2m"])}
        assert len(turbine["points"]) == horizon
        for i, row in enumerate(turbine["points"]):
            timestamp = datetime.fromisoformat(row["timestamp"])
            assert timestamp == start + timedelta(hours=i)
            assert (row["wind_speed"], row["temperature"]) == weather[timestamp]
            assert 0 <= row["lower_mw"] <= row["power_mw"] <= row["upper_mw"] <= capacity
            assert math.isclose(row["power_mw"], row["normalized_power"] * capacity)
            assert row["energy_mwh"] == row["power_mw"]
            if row["wind_speed"] < 2.5 or row["wind_speed"] > 25:
                assert row["power_mw"] == row["upper_mw"] == row["lower_mw"] == 0
        details.append({"turbine_id": tid, "energy_24h_mwh": sum(r["energy_mwh"] for r in turbine["points"][:24]),
                        "energy_horizon_mwh": sum(r["energy_mwh"] for r in turbine["points"]),
                        "first_hour_power_mw": turbine["points"][0]["power_mw"],
                        "physics_correction_counts": turbine["physics_correction_counts"]})
    for i, row in enumerate(result["farm"]["points"]):
        for key in ("power_mw", "lower_mw", "upper_mw", "energy_mwh"):
            assert math.isclose(row[key], sum(t["points"][i][key] for t in result["turbines"]))
    for key in ("energy_24h_mwh", "energy_horizon_mwh"):
        assert math.isclose(result["summary"][key], sum(t[key] for t in details))
    with (ROOT / "reports/live_forecast.csv").open(encoding="utf-8", newline="") as handle:
        exported = list(csv.DictReader(handle))
    assert len(exported) == horizon * 2
    for saved, row in zip(exported, [r for t in result["turbines"] for r in t["points"]]):
        assert float(saved["power_mw"]) == row["power_mw"] and saved["timestamp"] == row["timestamp"]
    return {"passed": True, "issued_at": result["issued_at"], "forecast_start": result["forecast_start"],
            "forecast_rows": len(exported), "summary": result["summary"], "turbines": details,
            "checks": ["source hashes and retrieval timestamps", "weather rows match original API response",
                       "future consecutive UTC hours", "2.5 MW capacity per turbine", "physics cut-in/out and interval bounds",
                       "independent farm MW/MWh sums", "CSV matches JSON"],
            "model_age_days": result["model"]["age_days"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    audit = verify(parser.parse_args().freeze)
    (ROOT / "reports/live_verification.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, indent=2))

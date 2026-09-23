"""Read-only model/data audit; optional comparison to an extracted user archive.

No source code from the compared archive is imported or executed. No models are
retrained, and shipped production artifacts are not scored as January holdouts.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windagent.data import load_hourly
from windagent.model import candidate_models, regression_metrics


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def constants(path: Path) -> dict:
    result = {}
    for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        result[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
    return result


def archived_hgb_parameters(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "candidate_models")
    call = next(n for n in ast.walk(function) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "HistGradientBoostingRegressor")
    return {arg.arg: ast.literal_eval(arg.value) for arg in call.keywords}


def verify(archive: Path | None = None) -> dict:
    metadata = json.loads((ROOT / "artifacts/metadata.json").read_text())
    frame = load_hourly(ROOT / "data/raw", metadata["timezone"],
                        min_samples=metadata["training_config"]["min_samples"],
                        timestamp_semantics=metadata["training_config"]["timestamp_semantics"])
    saved = pd.read_csv(ROOT / "reports/conditional_holdout_predictions.csv")
    saved["timestamp"] = pd.to_datetime(saved.timestamp, utc=True)
    assert not saved.duplicated(["turbine_id", "timestamp"]).any()
    actuals = frame[["turbine_id", "timestamp", "wind_speed", "power"]]
    saved = saved.merge(actuals, on=["turbine_id", "timestamp"], how="left", validate="one_to_one")
    assert saved.power.notna().all()
    np.testing.assert_allclose(saved.actual, saved.power, atol=1e-12)
    report = {"verified_at": datetime.now(timezone.utc).isoformat(),
              "model_version": metadata["model_version"], "turbines": {},
              "method": "Recompute archived pre-January holdout predictions; never score a production artifact fitted on January against January.",
              "limitations": ["Conditional observed-weather evaluation only.",
                              "No February actual power is present.",
                              "Timezone, interval convention and normalization formula remain unconfirmed."]}
    if archive:
        archive = archive.resolve()
        old = json.loads((archive / "artifacts/metadata.json").read_text())
        old_params = archived_hgb_parameters(archive / "windagent/model.py")
        current_params = candidate_models()["hist_gradient_boosting"].get_params()
        assert all(current_params[key] == value for key, value in old_params.items())
        report["archive_comparison"] = {
            "archive_model_version": old["model_version"],
            "archive_hgb_parameters": old_params, "hgb_reference_matches_archive_algorithm": True,
            "archive_training_uses_physics_feature": "calculate_physics_power_baseline" in (archive / "windagent/train.py").read_text(),
            "archive_physics_constants": {k: v for k, v in constants(archive / "src/physics.py").items() if k.endswith("SPEED")},
            "archive_capacity_configuration": json.loads((archive / "config/turbines.json").read_text()),
            "capacity_and_physics_interpretation": "Inherited configuration and code assertions; not independent confirmation of nameplate capacity, normalization or cut-in/cut-out thresholds.",
        }
    for turbine_id, part in saved.groupby("turbine_id"):
        key = str(int(turbine_id))
        item = metadata["turbines"][key]
        selected = item["selected_candidate"]
        source_path = ROOT / f"data/raw/turbine_{int(turbine_id)}.csv"
        assert digest(source_path) == metadata["input_hashes"][key]
        assert digest(ROOT / "artifacts" / item["artifact"]) == item["artifact_sha256"]
        assert (part.selected_candidate == selected).all()
        assert part.timestamp.min() == pd.Timestamp(item["chronological_split"]["holdout"]["first_hour_utc"])
        assert part.timestamp.max() == pd.Timestamp(item["chronological_split"]["holdout"]["last_hour_utc"])
        assert pd.Timestamp(item["training_last_hour_utc"]) + pd.Timedelta(hours=1) <= pd.Timestamp(metadata["model_available_at"])
        for fold in item["rolling_validation_folds"]:
            assert pd.Timestamp(fold["fit"]["last_hour_utc"]) < pd.Timestamp(fold["validation"]["first_hour_utc"])
            assert pd.Timestamp(fold["validation"]["last_hour_utc"]) < part.timestamp.min()
        chosen = min(item["candidate_validation_metrics"], key=lambda name: item["candidate_validation_metrics"][name]["mean_fold_mae"])
        assert chosen == selected
        metrics = {}
        for name in (selected, "hist_gradient_boosting", "wind_bin_curve"):
            result = regression_metrics(part.actual.to_numpy(), part[name].to_numpy())
            expected = item["holdout_candidates"][name]
            for metric in ("mae", "rmse", "bias", "r2"):
                assert abs(result[metric] - expected[metric]) < 1e-10
            metrics[name] = result
        raw = pd.read_csv(source_path)
        wind, target = raw.iloc[:, 2], raw.iloc[:, 3]
        low = wind < 2.5
        physics_alternative = np.where((part.wind_speed < 2.5) | (part.wind_speed > 25), 0, part[selected])
        record = {
            "raw_sha256": digest(source_path), "raw_rows": len(raw),
            "missing_raw_local_ten_minute_timestamps": frame.attrs["quality_report"][key]["missing_raw_local_ten_minute_timestamps"],
            "selected_candidate": selected, "january_hours": len(part), "metrics": metrics,
            "relative_mae_improvement_against_original_algorithm": 1 - metrics[selected]["mae"] / metrics["hist_gradient_boosting"]["mae"],
            "raw_threshold_diagnostic": {"rows_below_2_5_ms": int(low.sum()),
                                         "nonzero_normalized_targets_below_2_5_ms": int((target[low] > 0).sum()),
                                         "mean_normalized_target_below_2_5_ms": float(target[low].mean()),
                                         "rows_above_25_ms": int((wind > 25).sum()),
                                         "maximum_observed_wind_ms": float(wind.max())},
            "posthoc_fixed_archive_physics_rule_on_holdout": regression_metrics(part.actual.to_numpy(), physics_alternative),
            "physics_diagnostic_note": "Post-hoc audit of an inherited fixed rule, not another tuning or selection step; normalized zero is not proven equivalent to stopped generation.",
        }
        if archive:
            record["archive_raw_csv_identical"] = digest(archive / f"data/raw/turbine_{int(turbine_id)}.csv") == digest(source_path)
            assert record["archive_raw_csv_identical"]
            record["archive_artifact_sha256"] = digest(archive / "artifacts" / old["turbines"][key]["artifact"])
            assert record["archive_artifact_sha256"] == old["turbines"][key]["artifact_sha256"]
        report["turbines"][key] = record
    report["status"] = "passed_model_revision_audit"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Optional extracted wind-agent archive to inspect without executing its code")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/model_revision_audit.json")
    args = parser.parse_args()
    result = verify(args.archive)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"status": result["status"], "report": str(args.output),
                      "models": {k: {"chosen": v["selected_candidate"], "mae_improvement": v["relative_mae_improvement_against_original_algorithm"]} for k, v in result["turbines"].items()}}, indent=2))

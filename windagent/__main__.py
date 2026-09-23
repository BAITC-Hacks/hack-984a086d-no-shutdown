"""Command line entry points for training, replay and autonomous operation."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
from pathlib import Path


def _write_json(value: dict, path: str | Path | None = None) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str, allow_nan=False)
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"Saved {target.resolve()}")
    else:
        print(text)


def export_forecasts(results: list[dict], destination: Path) -> int:
    """Long format preserves forecast origin, turbine and horizon independently."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fields = ["forecast_id", "origin", "turbine_id", "timestamp", "lead_hour", "power", "lower", "upper"]
    count = 0
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            if result.get("status") != "succeeded":
                continue
            for turbine_id, series in result["turbines"].items():
                for lead, row in enumerate(series):
                    writer.writerow({"forecast_id": result["id"], "origin": result["origin"],
                                     "turbine_id": turbine_id, "lead_hour": lead,
                                     **{key: row[key] for key in ("timestamp", "power", "lower", "upper")}})
                    count += 1
    return count


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="HackAlem autonomous wind power forecast backend")
    result.add_argument("--home", type=Path, default=Path(os.environ.get("WINDAGENT_HOME", Path(__file__).resolve().parent.parent)))
    sub = result.add_subparsers(dest="command", required=True)
    train = sub.add_parser("train", help="Fit and validate models using only observations before cutoff")
    train.add_argument("--cutoff", default="2026-02-01T00:00:00+05:00")
    train.add_argument("--timezone", default="Asia/Almaty")
    train.add_argument("--data-dir", type=Path)
    train.add_argument("--artifact-dir", type=Path)
    train.add_argument("--timestamp-semantics", choices=("start", "end"), default="start")
    train.add_argument("--min-samples", type=int, default=6)
    train.add_argument("--holdout-start")
    train.add_argument("--validation-months", type=int, default=6)
    train.add_argument("--folds", type=int, default=3)
    train.add_argument("--config", type=Path)
    forecast = sub.add_parser("forecast", help="Run the complete agent cycle")
    forecast.add_argument("--origin", required=True, help="ISO timestamp with timezone offset")
    forecast.add_argument("--horizon", type=int, default=48)
    forecast.add_argument("--refresh", action="store_true")
    forecast.add_argument("--output")
    forecast.add_argument("--csv", type=Path)
    replay = sub.add_parser("replay", help="Run daily historical origins, recording every failure")
    replay.add_argument("--start", default="2026-02-01T00:00:00+05:00")
    replay.add_argument("--days", type=int, default=28)
    replay.add_argument("--horizon", type=int, default=48)
    replay.add_argument("--output", default="reports/february_replay.json")
    replay.add_argument("--csv", type=Path, default=Path("reports/february_forecasts.csv"))
    backtest = sub.add_parser("backtest", help="Train a separate pre-January model and score seven archived-weather origins")
    backtest.add_argument("--start", default="2026-01-01T00:00:00+05:00")
    backtest.add_argument("--days", type=int, default=7)
    watch = sub.add_parser("watch", help="Poll for new weather and recalculate changed inputs until Ctrl+C")
    watch.add_argument("--interval", type=float, default=300)
    watch.add_argument("--origin", help="Optional fixed historical origin; otherwise use current UTC hour")
    live = sub.add_parser("live", help="Fetch current forecast weather and predict with the trained models")
    live.add_argument("--horizon", type=int, choices=(24, 48), default=48)
    live.add_argument("--refresh", action="store_true")
    live.add_argument("--output", type=Path, default=Path("reports/live_forecast.json"))
    live.add_argument("--csv", type=Path, default=Path("reports/live_forecast.csv"))
    live_watch = sub.add_parser("live-watch", help="Check current weather and rerun the model when inputs change")
    live_watch.add_argument("--horizon", type=int, choices=(24, 48), default=48)
    live_watch.add_argument("--interval", type=float, default=300)
    serve = sub.add_parser("serve", help="Start the local API and interactive API documentation")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--fastapi", action="store_true", help="Use optional FastAPI/uvicorn and /docs")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    home = args.home.resolve()
    os.environ["WINDAGENT_HOME"] = str(home)
    from .settings import load_env
    load_env(home / ".env")
    try:
        if args.command == "train":
            from .train import train_models
            metadata = train_models(args.data_dir or home / "data/raw", args.artifact_dir or home / "artifacts",
                                    cutoff=args.cutoff, timezone=args.timezone,
                                    timestamp_semantics=args.timestamp_semantics, min_samples=args.min_samples,
                                    holdout_start=args.holdout_start, validation_months=args.validation_months,
                                    folds=args.folds, model_config=json.loads(args.config.read_text(encoding="utf-8")) if args.config else None)
            _write_json(metadata)
        elif args.command == "serve":
            if args.fastapi:
                import uvicorn
                uvicorn.run("windagent.api:app", host=args.host, port=args.port)
            else:
                from .server import serve
                serve(args.host, args.port)
        elif args.command in ("live", "live-watch"):
            from .live import LiveForecastAgent
            from .service import csv_content
            live_agent = LiveForecastAgent(home)
            if args.command == "live":
                result = live_agent.run(args.horizon, args.refresh)
                _write_json(result, home / args.output)
                csv_path = home / args.csv
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                csv_path.write_text(csv_content(live_agent.store.get(result["id"])), encoding="utf-8")
            else:
                if args.interval < 60:
                    raise ValueError("live-watch interval must be at least 60 seconds")
                stop = threading.Event()
                print(f"Live monitoring every {args.interval:g}s. Ctrl+C to stop.")
                try:
                    while not stop.is_set():
                        try:
                            result = live_agent.run(args.horizon, refresh=True)
                            print(f"Forecast {result['id']}: {result['forecast_start']}; reused={result.get('reused', False)}", flush=True)
                        except Exception as exc:
                            print(f"Live forecast failed: {exc}", file=sys.stderr, flush=True)
                        stop.wait(args.interval)
                except KeyboardInterrupt:
                    stop.set()
        elif args.command == "backtest":
            from .evaluate import run_january_backtest
            result = run_january_backtest(home / "data/raw", home / "evaluation/january",
                                         home / "data/weather", home / "reports/january_backtest.json",
                                         first_origin=args.start, origin_count=args.days)
            _write_json(result)
            return 1 if result["failures"] else 0
        else:
            from .agent import ForecastAgent
            agent = ForecastAgent(home)
            if args.command == "forecast":
                result = agent.run(args.origin, horizon=args.horizon, refresh=args.refresh)
                _write_json(result, args.output)
                if args.csv:
                    export_forecasts([result], args.csv)
            elif args.command == "replay":
                result = agent.replay(start=args.start, days=args.days, horizon=args.horizon)
                _write_json(result, home / args.output)
                rows = export_forecasts(result["origins"], home / args.csv)
                succeeded = sum(r["status"] == "succeeded" for r in result["origins"])
                print(f"Replay: {succeeded}/{args.days} origins succeeded; {rows} turbine forecast rows. {result['metrics_note']}")
                return 0 if result["status"] == "succeeded" else 1
            else:
                stop = threading.Event()
                provider = (lambda: args.origin) if args.origin else None
                print(f"Watching weather every {args.interval:g}s. Press Ctrl+C to stop.")
                try:
                    agent.run_loop(stop, interval_seconds=args.interval, origin_provider=provider)
                except KeyboardInterrupt:
                    stop.set()
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

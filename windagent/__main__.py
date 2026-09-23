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
    serve = sub.add_parser("serve", help="Start the local API and interactive API documentation")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    home = args.home.resolve()
    os.environ["WINDAGENT_HOME"] = str(home)
    try:
        if args.command == "train":
            from .train import train_models
            metadata = train_models(args.data_dir or home / "data/raw", args.artifact_dir or home / "artifacts",
                                    cutoff=args.cutoff, timezone=args.timezone)
            _write_json(metadata)
        elif args.command == "serve":
            import uvicorn
            uvicorn.run("windagent.api:app", host=args.host, port=args.port)
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

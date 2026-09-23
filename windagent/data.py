"""Loading and hourly aggregation for the supplied turbine observations."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd


CSV_COLUMNS = {
    "Статистическое время": "local_timestamp",
    "Средняя скорость ветра(m/s)": "wind_speed",
    "Нормализованная активная мощность": "power",
    "Средняя температура окружающей среды(°C)": "temperature",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_hourly(data_dir: Path, timezone: str = "Asia/Almaty") -> pd.DataFrame:
    """Read both raw CSVs and return adequately-covered hourly observations.

    Input timestamps are interpreted as local site time, then converted to UTC.
    An hourly bin is retained only if it has at least four distinct valid
    ten-minute starts. No values are interpolated. A quality summary is attached
    to ``DataFrame.attrs['quality_report']`` for training reports.
    """
    root = Path(data_dir)
    outputs: list[pd.DataFrame] = []
    report: dict[str, dict[str, int | str]] = {}
    for turbine_id in (1, 2):
        path = root / f"turbine_{turbine_id}.csv"
        raw = pd.read_csv(path)
        missing_columns = set(CSV_COLUMNS).difference(raw.columns)
        if missing_columns:
            raise ValueError(f"{path} is missing required columns: {sorted(missing_columns)}")
        frame = raw[list(CSV_COLUMNS)].rename(columns=CSV_COLUMNS).copy()
        frame["local_timestamp"] = pd.to_datetime(frame["local_timestamp"], errors="coerce")
        for column in ("wind_speed", "power", "temperature"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

        parse_bad = frame["local_timestamp"].isna()
        localized = frame["local_timestamp"].dt.tz_localize(
            timezone, ambiguous="NaT", nonexistent="NaT"
        )
        timestamp_bad = localized.isna()
        numeric_bad = ~frame[["wind_speed", "power", "temperature"]].apply(
            lambda col: col.map(lambda value: pd.notna(value) and abs(float(value)) != float("inf"))
        ).all(axis=1)
        off_grid = localized.notna() & (
            localized.dt.minute.mod(10).ne(0) | localized.dt.second.ne(0) | localized.dt.microsecond.ne(0)
        )
        range_bad = (
            (frame["wind_speed"] < 0) | (frame["wind_speed"] > 100)
            | (frame["power"] < 0) | (frame["power"] > 1)
            | (frame["temperature"] < -80) | (frame["temperature"] > 70)
        )
        valid = ~(timestamp_bad | numeric_bad | range_bad | off_grid)
        clean = frame.loc[valid, ["wind_speed", "power", "temperature"]].copy()
        clean["timestamp"] = localized.loc[valid].dt.tz_convert("UTC")
        before_dedup = len(clean)
        clean = clean.sort_values("timestamp").drop_duplicates("timestamp", keep="first")
        duplicate_count = before_dedup - len(clean)
        clean["hour"] = clean["timestamp"].dt.floor("h")
        grouped = clean.groupby("hour", sort=True)
        hourly = grouped[["wind_speed", "power", "temperature"]].mean()
        hourly["sample_count"] = grouped["timestamp"].nunique().astype("int64")
        hourly = hourly.loc[hourly["sample_count"] >= 4].reset_index().rename(columns={"hour": "timestamp"})
        hourly.insert(0, "turbine_id", turbine_id)
        outputs.append(hourly[["turbine_id", "timestamp", "wind_speed", "temperature", "power", "sample_count"]])
        report[str(turbine_id)] = {
            "raw_rows": int(len(raw)),
            "invalid_timestamp_rows": int(parse_bad.sum()),
            "invalid_or_dst_timestamp_rows": int((timestamp_bad & ~parse_bad).sum()),
            "off_grid_timestamp_rows": int(off_grid.sum()),
            "invalid_numeric_or_range_rows": int((numeric_bad | range_bad).sum()),
            "duplicate_timestamp_rows": int(duplicate_count),
            "valid_unique_ten_minute_rows": int(len(clean)),
            "retained_hourly_rows": int(len(hourly)),
            "dropped_undercovered_hours": int((grouped.size() < 4).sum()),
            "raw_sha256": sha256_file(path),
            "first_valid_local_timestamp": str(clean["timestamp"].min().tz_convert(timezone)) if len(clean) else None,
            "last_valid_local_timestamp": str(clean["timestamp"].max().tz_convert(timezone)) if len(clean) else None,
        }
    result = pd.concat(outputs, ignore_index=True).sort_values(["turbine_id", "timestamp"]).reset_index(drop=True)
    result.attrs["quality_report"] = report
    result.attrs["timezone_assumption"] = timezone
    return result

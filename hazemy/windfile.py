"""v2/wind.json: 850 hPa wind for the app's slider, plus five days of fires (docs/schema-wind.md).

Built from the same cached GFS slices as the v2 paths, reaching 96 h back and 78 h ahead of
"now". The app runs the back-trajectory itself from these fields, so `decode` returns exactly
what `trajectory.back_trajectories` takes: it is the Python reference for the app's port.
"""
from __future__ import annotations

import base64
import gzip
import json
import math
from collections import Counter
from datetime import datetime, timedelta

import numpy as np

from hazemy import drivers, firms, gfs, trajectory
from hazemy.common import PipelineError, STEP, floor_hours, fmt, parse_time

SCHEMA = "hazemy-wind/1"
LEVEL_HPA = 850
SCALE_MS = 0.5
STEP_H = 3
BACK_H = 96
# 78, not 72: the app's third Wind day arrives at "now" snapped to the MYT 3 h grid + 72 h,
# which can be up to 6 h after start_utc when the feed is a few hours old.
AHEAD_H = 78
LAT_FIRST, LON_FIRST, STEP_DEG = -12.0, 90.0, 0.5
NLAT, NLON = 53, 71
FIELD_BYTES = NLAT * NLON
FIRE_DAYS = 5
MAX_BYTES = 800_000
GRID = {"lat_first": LAT_FIRST, "lon_first": LON_FIRST, "step_deg": STEP_DEG,
        "nlat": NLAT, "nlon": NLON}
DECODED_GEOMETRY = {"ni": NLON, "nj": NLAT, "lat_first": LAT_FIRST, "lon_first": LON_FIRST,
                    "di": STEP_DEG, "dj": STEP_DEG, "south_first": True}


def slice_times(start: datetime) -> list:
    """Every 3 h from the step at or before start - 96 h to the first at or after start + 78 h."""
    first = floor_hours(start - timedelta(hours=BACK_H), STEP_H)
    last = start + timedelta(hours=AHEAD_H)
    times = [first]
    while times[-1] < last:
        times.append(times[-1] + STEP)
    return times


def _axis_indices(first: float, step: float, count: int, targets: np.ndarray) -> np.ndarray:
    """Index of each target on the axis first + k * step (k = 0..count-1); exact matches only."""
    index = np.rint((targets - first) / step).astype(int)
    if index.min() < 0 or index.max() >= count or not np.allclose(
            first + index * step, targets, atol=1e-6):
        raise PipelineError("GFS 850 hPa field lacks the wind.json grid points "
                            f"({targets.min()} to {targets.max()}).")
    return index


def regrid(field, geometry: dict) -> np.ndarray:
    """The wind.json grid (south to north, west to east) cut from a cropped GFS field."""
    sign = 1 if geometry["south_first"] else -1
    lats = LAT_FIRST + STEP_DEG * np.arange(NLAT)
    lons = LON_FIRST + STEP_DEG * np.arange(NLON)
    rows = _axis_indices(geometry["lat_first"], sign * geometry["dj"], geometry["nj"], lats)
    cols = _axis_indices(geometry["lon_first"], geometry["di"], geometry["ni"], lons)
    return np.asarray(field)[np.ix_(rows, cols)]


def encode_field(values: np.ndarray) -> str:
    """Base64 of the int8 field (value / SCALE_MS, rounded and clipped to +/-127)."""
    if values.shape != (NLAT, NLON) or not np.isfinite(values).all():
        raise PipelineError(f"wind field must be a finite {NLAT}x{NLON} array, "
                            f"got shape {values.shape}")
    ints = np.clip(np.rint(values / SCALE_MS), -127, 127).astype(np.int8)
    return base64.b64encode(ints.tobytes()).decode("ascii")


def decode_field(text: str) -> np.ndarray:
    """(NLAT, NLON) float array in m/s from a base64 int8 field."""
    raw = base64.b64decode(text, validate=True)
    if len(raw) != FIELD_BYTES:
        raise PipelineError(f"wind field is {len(raw)} bytes, expected {FIELD_BYTES}")
    return np.frombuffer(raw, dtype=np.int8).reshape(NLAT, NLON).astype(float) * SCALE_MS


def load_times(start: datetime, newest: datetime, cache_dir, budget, fetch, log) -> tuple:
    """([time entry dicts], cycle used): the newest cycle, else the one 6 h before it."""
    times = slice_times(start)
    last_error = None
    for cycle in (newest, newest - timedelta(hours=gfs.CYCLE_H)):
        try:
            fields = drivers.load_wind850(times, cycle, cache_dir, budget, fetch, log)
        except PipelineError as err:
            last_error = err
            log(f"GFS 850 hPa wind from cycle {cycle:%Y-%m-%d %H} UTC unusable for wind.json: "
                f"{err}")
            continue
        plan = gfs.plan_steps(times, cycle)
        return [_time_entry(valid, src_cycle, fhr, fields[valid])
                for valid, src_cycle, fhr in plan], cycle
    raise PipelineError(f"No usable GFS cycle for wind.json: {last_error}")


def _time_entry(valid: datetime, cycle: datetime, fhr: int, field: tuple) -> dict:
    u, v, geometry = field
    return {"valid_utc": fmt(valid), "cycle_utc": fmt(cycle), "fhr": fhr,
            "u": encode_field(regrid(u, geometry)), "v": encode_field(regrid(v, geometry))}


def fire_days(rows: list, located: list, end: datetime) -> tuple:
    """(provinces, days): five 24 h windows ending at `end`, newest first, binned at 0.25 deg.

    A bin's `p` indexes `provinces` (the most common province of its detections) or is -1.
    Day 0 also keeps detections newer than `end` (the hour the slider calls "now"), so the
    "latest fires" shown for future steps never miss the newest overpass.
    """
    chosen = []
    for k in range(FIRE_DAYS):
        top, bottom = end - timedelta(hours=24 * k), end - timedelta(hours=24 * (k + 1))
        keep = [n for n, r in enumerate(rows) if bottom < r[2] and (k == 0 or r[2] <= top)]
        chosen.append(_day_bins([rows[n] for n in keep], [located[n] for n in keep], end))
    used = sorted({name for bins in chosen for _, name in bins if name})
    index = {name: n for n, name in enumerate(used)}
    days = []
    for k, bins in enumerate(chosen):
        days.append({
            "start_utc": fmt(end - timedelta(hours=24 * (k + 1))),
            "end_utc": fmt(end - timedelta(hours=24 * k)),
            "bins": [[*b, index[name] if name else -1] for b, name in bins]})
    return [list(name) for name in used], days


def _day_bins(rows: list, located: list, end: datetime) -> list:
    """[([lat, lon, count], most common (province, country) or None)] in fire_bins order."""
    det = drivers.Detections(rows, located, end)
    names = {}
    for lat, lon, place in zip(det.lat, det.lon, located):
        key = (math.floor(lat / drivers.BIN_DEG), math.floor(lon / drivers.BIN_DEG))
        names.setdefault(key, Counter())[place] += 1
    out = []
    for lat, lon, count in drivers.fire_bins(det):
        votes = names[(math.floor(lat / drivers.BIN_DEG), math.floor(lon / drivers.BIN_DEG))]
        named = {place: n for place, n in votes.items() if place}
        top = min(named, key=lambda p: (-named[p], p)) if named else None
        out.append(([lat, lon, count], top))
    return out


def build(*, now: datetime, newest: datetime, firms_updated, rows: list, provinces: list,
          cache_dir, budget, fetch, log=print) -> dict:
    """The wind.json document (not yet validated)."""
    start = trajectory.start_time(now)
    times, cycle = load_times(start, newest, cache_dir, budget, fetch, log)
    located = firms.locate_provinces([(r[0], r[1]) for r in rows], provinces)
    names, days = fire_days(rows, located, start)
    return {"schema": SCHEMA, "generated_utc": fmt(now), "start_utc": fmt(start),
            "gfs_cycle_utc": fmt(cycle), "level_hpa": LEVEL_HPA, "grid": dict(GRID),
            "scale_ms": SCALE_MS, "step_h": STEP_H, "times": times,
            "fires_updated_utc": fmt(firms_updated) if firms_updated else None,
            "provinces": names, "fires_days": days}


def _need(cond: bool, message: str) -> None:
    if not cond:
        raise PipelineError(f"wind.json invalid: {message}")


def _check_times(doc: dict) -> None:
    start = parse_time(doc["start_utc"])
    times = doc["times"]
    _need(bool(times), "no times")
    valid = [parse_time(t["valid_utc"]) for t in times]
    _need(all(b - a == STEP for a, b in zip(valid, valid[1:])),
          "times are not contiguous at 3 h")
    _need(valid[0] <= start - timedelta(hours=BACK_H), "times start after start - 96 h")
    _need(valid[-1] >= start + timedelta(hours=AHEAD_H), "times end before start + 78 h")
    for t, moment in zip(times, valid):
        for comp in ("u", "v"):
            try:
                decode_field(t[comp])
            except (ValueError, PipelineError) as err:
                raise PipelineError(f"wind.json invalid: {t['valid_utc']} {comp}: {err}") from err
        parse_time(t["cycle_utc"])
        _need(isinstance(t["fhr"], int) and t["fhr"] >= 0, f"bad fhr at {moment}")


def _check_fires(doc: dict) -> None:
    days, provinces = doc["fires_days"], doc["provinces"]
    _need(len(days) == FIRE_DAYS, f"{len(days)} fire days, expected {FIRE_DAYS}")
    for day in days:
        parse_time(day["start_utc"])
        parse_time(day["end_utc"])
        _need(len(day["bins"]) <= drivers.MAX_BINS, "too many fire bins in a day")
        for b in day["bins"]:
            _need(len(b) == 4, f"fire bin {b} is not [lat, lon, count, p]")
            _need(-1 <= b[3] < len(provinces), f"fire bin province index {b[3]} out of range")


def validate(doc: dict) -> bytes:
    """The compact JSON body, after checking the contract; raises PipelineError otherwise."""
    _need(doc.get("schema") == SCHEMA, f"schema is {doc.get('schema')!r}")
    _need(doc.get("level_hpa") == LEVEL_HPA, "level_hpa is not 850")
    _need(doc.get("grid") == GRID, f"grid is {doc.get('grid')}")
    _need(doc.get("scale_ms") == SCALE_MS and doc.get("step_h") == STEP_H,
          "scale_ms or step_h differs from the contract")
    try:
        _check_times(doc)
        _check_fires(doc)
    except (KeyError, TypeError, ValueError) as err:
        raise PipelineError(f"wind.json invalid: {type(err).__name__}: {err}") from err
    body = json.dumps(doc, separators=(",", ":")).encode()
    _need(len(body) < MAX_BYTES, f"{len(body)} bytes, limit {MAX_BYTES}")
    return body


def decode(doc: dict) -> tuple:
    """({valid: (u, v, geometry)}, times): what trajectory.back_trajectories takes."""
    fields, times = {}, []
    for t in doc["times"]:
        valid = parse_time(t["valid_utc"])
        fields[valid] = (decode_field(t["u"]), decode_field(t["v"]), dict(DECODED_GEOMETRY))
        times.append(valid)
    return fields, times


# The app's transfer budget for this file (v1.8 acceptance 7). Going over it breaks nothing,
# so it is a logged warning, not a failure: a peak fire season can push the fire days past it.
GZIP_TARGET_BYTES = 300_000


def size_line(path: str, body: bytes) -> str:
    packed = len(gzip.compress(body, compresslevel=9))
    line = f"wrote {path} ({len(body)} bytes, {packed} bytes gzip)"
    if packed > GZIP_TARGET_BYTES:
        line += f" WARNING: over the {GZIP_TARGET_BYTES} byte gzip target"
    return line

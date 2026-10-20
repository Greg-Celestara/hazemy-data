"""The "drivers" of v2: 850 hPa wind, rain tonight, and fires along the air's path."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

import numpy as np

from hazemy import compute, gfs, gfs_extra
from hazemy.common import PipelineError, floor_hours, myt_day_bounds
from hazemy.trajectory import KM_PER_DEG_LAT

PATH_BUFFER_KM = 25.0
TREND_RISING, TREND_FALLING = 1.2, 0.8
BIN_DEG = 0.25
MAX_BINS = 1500
RAIN_START_H, RAIN_END_H = 18, 30  # MYT hours counted from the start of today (18:00 to 06:00)


def load_wind850(valid_times: list, newest: datetime, cache_dir: str, budget, fetch,
                 log=print) -> dict:
    """{valid: (u, v, geometry)} of 850 hPa wind, each file fetched once into the cache."""
    fields = {}
    for valid, cycle, fhr in gfs.plan_steps(sorted(valid_times), newest):
        path = gfs_extra.cached_bundle(cache_dir, cycle, fhr, "w850", budget, fetch, log)
        (u, geometry), (v, _) = gfs_extra.decode_bundle(path, 2)
        fields[valid] = (u, v, geometry)
    return fields


def sample_wind(fields: dict, valid: datetime, lats, lons) -> tuple:
    u, v, geometry = fields[valid]
    return gfs.bilinear(u, geometry, lats, lons), gfs.bilinear(v, geometry, lats, lons)


def wind850_means(fields: dict, steps: list, lats, lons) -> list:
    """Per cell {'from_deg', 'speed_kmh'}: vector mean of the steps (direction the wind is FROM)."""
    missing = [t for t in steps if t not in fields]
    if missing:
        raise PipelineError("GFS 850 hPa wind lacks " + ", ".join(f"{t:%d %H}Z" for t in missing))
    sampled = [sample_wind(fields, t, lats, lons) for t in steps]
    us = np.array([s[0] for s in sampled])
    vs = np.array([s[1] for s in sampled])
    out = []
    for k in range(us.shape[1]):
        w = compute.wind_vector(us[:, k], vs[:, k])
        out.append({"from_deg": w["from_deg"], "speed_kmh": w["speed_kmh"]})
    return out


def _accumulated(hour: float, by_fhr: dict):
    """Per-cell precipitation accumulated since the cycle start, linear between 3 h steps."""
    low = int(hour // 3) * 3
    frac = (hour - low) / 3
    a = by_fhr[low]
    return a if frac == 0 else a * (1 - frac) + by_fhr[low + 3] * frac


def rain_window(now: datetime) -> tuple:
    start = myt_day_bounds(now, 0)[0]
    return start + timedelta(hours=RAIN_START_H), start + timedelta(hours=RAIN_END_H)


def rain_tonight(now: datetime, newest: datetime, lats, lons, cache_dir: str, budget, fetch,
                 log=print) -> list:
    """Per cell mm of GFS rain 18:00 today to 06:00 tomorrow MYT, 1 decimal.

    Uses the newest cycle that started no later than the window, so the window never begins
    before the cycle; falls back to the cycle before when a file is missing.
    """
    start, end = rain_window(now)
    cycle = min(newest, floor_hours(start, gfs.CYCLE_H))
    last_error = None
    for cyc in (cycle, cycle - timedelta(hours=gfs.CYCLE_H)):
        try:
            return _rain_from_cycle(cyc, start, end, lats, lons, cache_dir, budget, fetch, log)
        except PipelineError as err:
            last_error = err
            log(f"GFS rain from cycle {cyc:%Y-%m-%d %H} UTC unusable: {err}")
    raise PipelineError(f"No usable GFS cycle for rain: {last_error}")


def _rain_from_cycle(cycle, start, end, lats, lons, cache_dir, budget, fetch, log) -> list:
    h_start = (start - cycle) / timedelta(hours=1)
    h_end = (end - cycle) / timedelta(hours=1)
    needed = {int(h // 3) * 3 + d for h in (h_start, h_end) for d in (0, 3)}
    by_fhr = {0: np.zeros(len(lats))}
    for fhr in sorted(needed - {0}):
        path = gfs_extra.cached_bundle(cache_dir, cycle, fhr, "rain", budget, fetch, log)
        ((field, geometry),) = gfs_extra.decode_bundle(path, 1)
        by_fhr[fhr] = gfs.bilinear(field, geometry, lats, lons)
    rain = np.maximum(_accumulated(h_end, by_fhr) - _accumulated(h_start, by_fhr), 0.0)
    return [compute.round_pm(float(v)) for v in rain]


class Detections:
    """FIRMS detections of the last 72 h with their age and province."""

    def __init__(self, rows: list, located: list, now: datetime):
        self.lat = np.array([r[0] for r in rows], dtype=float)
        self.lon = np.array([r[1] for r in rows], dtype=float)
        self.age_h = np.array([(now - r[2]) / timedelta(hours=1) for r in rows], dtype=float)
        self.province = located


def _trend(recent: int, prior: int) -> str:
    if recent > TREND_RISING * prior:
        return "rising"
    if recent < TREND_FALLING * prior:
        return "falling"
    return "steady"


def path_fire_stats(path: list, det: Detections) -> dict:
    """Fire count within PATH_BUFFER_KM of any path point, its main province and its trend."""
    empty = {"path_fire_province": None, "path_fire_count_72h": 0, "path_fire_trend": None}
    if not len(det.lat):
        return empty
    pts = np.array(path)
    margin = PATH_BUFFER_KM / KM_PER_DEG_LAT * 1.5
    cand = np.where((det.lat >= pts[:, 0].min() - margin) & (det.lat <= pts[:, 0].max() + margin)
                    & (det.lon >= pts[:, 1].min() - margin * 2)
                    & (det.lon <= pts[:, 1].max() + margin * 2))[0]
    if not cand.size:
        return empty
    d_lat = (det.lat[cand][:, None] - pts[None, :, 0]) * KM_PER_DEG_LAT
    d_lon = ((det.lon[cand][:, None] - pts[None, :, 1]) * KM_PER_DEG_LAT
             * np.cos(np.radians(pts[None, :, 0])))
    near = cand[(np.hypot(d_lat, d_lon) <= PATH_BUFFER_KM).any(axis=1)]
    if not near.size:
        return empty
    ages = det.age_h[near]
    recent, prior = int((ages < 24).sum()), int(((ages >= 24) & (ages < 48)).sum())
    names = Counter(det.province[k][0] for k in near if det.province[k])
    province = min(names, key=lambda n: (-names[n], n)) if names else None
    return {"path_fire_province": province, "path_fire_count_72h": int(near.size),
            "path_fire_trend": _trend(recent, prior)}


def fires_daily(fires_72h: list, det: Detections) -> list:
    """Counts [48-72 h ago, 24-48 h ago, last 24 h] for the provinces of `fires_72h`."""
    out = []
    for entry in fires_72h:
        key = (entry["province"], entry["country"])
        ages = np.array([det.age_h[k] for k, p in enumerate(det.province) if p == key])
        counts = [int(((ages >= 48)).sum()), int(((ages >= 24) & (ages < 48)).sum()),
                  int((ages < 24).sum())]
        out.append({"province": entry["province"], "country": entry["country"],
                    "counts": counts})
    return out


def fire_bins(det: Detections) -> list:
    """[[bin centre lat, lon, count]] on a 0.25 degree grid, at most MAX_BINS, largest first."""
    if not len(det.lat):
        return []
    rows = np.floor(det.lat / BIN_DEG).astype(int)
    cols = np.floor(det.lon / BIN_DEG).astype(int)
    keys, counts = np.unique(np.stack([rows, cols], axis=1), axis=0, return_counts=True)
    order = sorted(range(len(counts)), key=lambda k: (-counts[k], keys[k][0], keys[k][1]))
    def centre(index: int) -> float:
        return round((int(index) + 0.5) * BIN_DEG, 3)

    return [[centre(keys[k][0]), centre(keys[k][1]), int(counts[k])] for k in order[:MAX_BINS]]

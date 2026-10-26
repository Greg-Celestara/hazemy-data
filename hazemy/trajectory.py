"""Kinematic 850 hPa back-trajectories from 3-hourly GFS wind slices.

Wind is bilinear in space and linear in time between the 3-hourly slices; the position is
integrated backwards in 1 h steps with Heun's method (RK2). All cells are integrated together.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from hazemy.common import PipelineError, STEP, floor_hours
from hazemy.gfs import CYCLE_H, bilinear

POINTS = 17            # index 0 = start (now, rounded down to the hour), index 16 = 48 h earlier
HOURS_PER_POINT = 3
BOX = (90.0, 125.0, -12.0, 14.0)  # west, east, south, north: a path stops when it leaves this
KM_PER_DEG_LAT = 6371.0 * math.pi / 180.0
MS_TO_KMH = 3.6
# The path starts at "now" (the hour of generation), which is 5 to 10 h after the newest cycle
# (it is published about 5 h late), and runs 48 h back: so the data must reach back to 43 h before
# the newest cycle. Each cycle gives f000 and f003, so 8 cycles reach only 42 h; the 9th supplies
# the rest.
N_CYCLES = 9


def start_time(now: datetime) -> datetime:
    """The instant the air arrives at the cell: `now` rounded down to the hour (UTC)."""
    return floor_hours(now, 1)


def trajectory_cycles(newest: datetime) -> list:
    """The cycles whose f000/f003 slices feed the path, oldest first."""
    return [newest - timedelta(hours=CYCLE_H * k) for k in range(N_CYCLES - 1, -1, -1)]


def slice_times(cycles: list, start: datetime) -> list:
    """Valid times of the wind slices, ascending and 3-hourly.

    f000 and f003 of every cycle, then forecast steps (f006, f009, ...) of the newest cycle up
    to the first one at or after `start`, because the newest analysis is hours behind "now".
    """
    times = []
    for cycle in cycles:
        times += [cycle, cycle + STEP]
    nxt = cycles[-1] + STEP
    while nxt < start:
        nxt += STEP
        times.append(nxt)
    return sorted(set(times))


def point_times(start: datetime) -> list:
    """The time each point of a path refers to, newest first."""
    return [start - timedelta(hours=HOURS_PER_POINT * k) for k in range(POINTS)]


def wind_at(fields: dict, times: list, position: float, lats, lons):
    """(u, v) in m/s at points; `position` is the fractional index into `times` (3 h apart)."""
    lo = int(min(max(math.floor(position), 0), len(times) - 2))
    w = position - lo
    out = []
    for comp in (0, 1):
        a, geom = fields[times[lo]][comp], fields[times[lo]][2]
        b = fields[times[lo + 1]][comp]
        out.append((1 - w) * bilinear(a, geom, lats, lons) + w * bilinear(b, geom, lats, lons))
    return out[0], out[1]


def _move(lats, lons, u, v, hours):
    """New positions after `hours` (negative = backwards) of constant wind u, v (m/s)."""
    km_lat = v * MS_TO_KMH * hours
    km_lon = u * MS_TO_KMH * hours
    new_lat = lats + km_lat / KM_PER_DEG_LAT
    new_lon = lons + km_lon / (KM_PER_DEG_LAT * np.cos(np.radians(lats)))
    return new_lat, new_lon


def _inside(lats, lons):
    west, east, south, north = BOX
    return (lons >= west) & (lons <= east) & (lats >= south) & (lats <= north)


def back_trajectories(fields: dict, times: list, lats, lons, start: datetime = None, *,
                      every_h: int = HOURS_PER_POINT) -> list:
    """One list of (lat, lon) per start point, newest first, at most POINTS long.

    `every_h` is the spacing of the recorded points; 1 gives hourly points (49 of them).

    `fields` is {valid: (u, v, geometry)}; the air arrives at `start` (default: the last slice
    in `times`) and `times` must cover 48 h before it. A path ends before the first point
    outside BOX.
    """
    if every_h < 1 or (POINTS - 1) * HOURS_PER_POINT % every_h:
        raise PipelineError(f"every_h must divide 48 h, got {every_h}.")
    last = len(times) - 1
    start_pos = last if start is None else (start - times[0]) / STEP
    if start_pos > last + 1e-9 or start_pos - (POINTS - 1) < -1e-9:
        raise PipelineError(f"Back-trajectories from {start} need {POINTS} slices up to it, "
                            f"got {len(times)} from {times[0]} to {times[-1]}.")
    lats, lons = np.asarray(lats, float), np.asarray(lons, float)
    paths = [[(float(a), float(b))] for a, b in zip(lats, lons)]
    alive = np.ones(len(lats), dtype=bool)
    for hour in range(POINTS * HOURS_PER_POINT - HOURS_PER_POINT):
        pos = start_pos - hour / HOURS_PER_POINT
        u1, v1 = wind_at(fields, times, pos, lats, lons)
        trial_lat, trial_lon = _move(lats, lons, u1, v1, -1.0)
        u2, v2 = wind_at(fields, times, pos - 1 / HOURS_PER_POINT, trial_lat, trial_lon)
        new_lat, new_lon = _move(lats, lons, (u1 + u2) / 2, (v1 + v2) / 2, -1.0)
        inside = _inside(new_lat, new_lon)
        alive &= inside
        # A stopped path keeps integrating from its last position so arrays stay in the grid.
        lats = np.where(alive, new_lat, lats)
        lons = np.where(alive, new_lon, lons)
        if (hour + 1) % every_h == 0:
            for k in np.where(alive)[0]:
                paths[k].append((float(lats[k]), float(lons[k])))
    return paths


def rounded(path: list) -> list:
    return [[round(lat, 2), round(lon, 2)] for lat, lon in path]


def bearing_deg(a: tuple, b: tuple) -> float:
    """Initial compass bearing from a to b (small-distance flat approximation)."""
    d_lat = b[0] - a[0]
    d_lon = (b[1] - a[1]) * math.cos(math.radians(a[0]))
    return math.degrees(math.atan2(d_lon, d_lat)) % 360


def distance_km(a: tuple, b: tuple) -> float:
    d_lat = b[0] - a[0]
    d_lon = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(d_lat, d_lon) * KM_PER_DEG_LAT

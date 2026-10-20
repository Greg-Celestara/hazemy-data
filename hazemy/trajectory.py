"""Kinematic 850 hPa back-trajectories from 3-hourly GFS wind slices.

Wind is bilinear in space and linear in time between the 3-hourly slices; the position is
integrated backwards in 1 h steps with Heun's method (RK2). All cells are integrated together.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from hazemy.common import PipelineError, STEP
from hazemy.gfs import CYCLE_H, bilinear

POINTS = 17            # index 0 = start, index 16 = 48 h earlier
HOURS_PER_POINT = 3
BOX = (90.0, 125.0, -12.0, 14.0)  # west, east, south, north: a path stops when it leaves this
KM_PER_DEG_LAT = 6371.0 * math.pi / 180.0
MS_TO_KMH = 3.6
# 17 points need 48 h of data behind the newest slice (cycle + 3 h). Each cycle gives f000 and
# f003, so 8 cycles reach only 45 h; the 9th supplies the missing 3 h.
N_CYCLES = 9


def trajectory_cycles(newest: datetime) -> list:
    """The cycles whose f000/f003 slices feed the path, oldest first."""
    return [newest - timedelta(hours=CYCLE_H * k) for k in range(N_CYCLES - 1, -1, -1)]


def slice_times(cycles: list) -> list:
    """Valid times of f000 and f003 of every cycle, ascending."""
    times = []
    for cycle in cycles:
        times += [cycle, cycle + STEP]
    return sorted(set(times))


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


def back_trajectories(fields: dict, times: list, lats, lons) -> list:
    """One list of (lat, lon) per start point, newest first, at most POINTS long.

    `fields` is {valid: (u, v, geometry)}; the start is the last slice in `times`. A path ends
    before the first point outside BOX.
    """
    if len(times) < POINTS:
        raise PipelineError(f"Back-trajectories need {POINTS} slices, got {len(times)}.")
    lats, lons = np.asarray(lats, float), np.asarray(lons, float)
    paths = [[(float(a), float(b))] for a, b in zip(lats, lons)]
    alive = np.ones(len(lats), dtype=bool)
    last = len(times) - 1
    for hour in range(POINTS * HOURS_PER_POINT - HOURS_PER_POINT):
        pos = last - hour / HOURS_PER_POINT
        u1, v1 = wind_at(fields, times, pos, lats, lons)
        trial_lat, trial_lon = _move(lats, lons, u1, v1, -1.0)
        u2, v2 = wind_at(fields, times, pos - 1 / HOURS_PER_POINT, trial_lat, trial_lon)
        new_lat, new_lon = _move(lats, lons, (u1 + u2) / 2, (v1 + v2) / 2, -1.0)
        inside = _inside(new_lat, new_lon)
        alive &= inside
        # A stopped path keeps integrating from its last position so arrays stay in the grid.
        lats = np.where(alive, new_lat, lats)
        lons = np.where(alive, new_lon, lons)
        if (hour + 1) % HOURS_PER_POINT == 0:
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

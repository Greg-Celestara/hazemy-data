"""Turn sampled CAMS and GFS series into the per-cell numbers of the contract."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from hazemy.common import (MYT, PipelineError, STEP, floor_hours, myt_day_bounds,
                           steps_between)
from hazemy.doe import band, compass, pm25_to_api, round_half_up

WINDOW_STEPS = 8  # 8 steps of 3 h: the 24 h mean, with CAMS available every 3 h
FORECAST_DAYS = 3  # tomorrow plus the next two days
MS_TO_KMH = 3.6
CALM_KMH = 0.5  # below this the direction is meaningless; reported as from 0 deg (N)


def window_steps(end: datetime) -> list:
    """The 8 3-hourly instants of the trailing 24 h window ending at `end`."""
    return [end - STEP * k for k in range(WINDOW_STEPS - 1, -1, -1)]


def mean_of(series: dict, times: list):
    """Mean over `times` of per-cell arrays; raises naming the missing times."""
    missing = [t for t in times if t not in series]
    if missing:
        raise PipelineError("CAMS timeline lacks " + ", ".join(f"{t:%d %H}Z" for t in missing))
    return np.mean([series[t] for t in times], axis=0)


def now_end(now: datetime) -> datetime:
    """Latest 3-hourly step at or before `now`."""
    return floor_hours(now, 3)


def tomorrow_steps(now: datetime) -> list:
    """The 8 steps inside tomorrow's MYT calendar day (16:00 UTC to 16:00 UTC)."""
    return steps_between(*myt_day_bounds(now, 1))


def day_steps(now: datetime, offset: int) -> list:
    """The 8 steps inside the MYT calendar day `offset` days after today's."""
    return steps_between(*myt_day_bounds(now, offset))


def day_date(now: datetime, offset: int) -> str:
    """ISO date of that MYT day."""
    return myt_day_bounds(now, offset)[0].astimezone(MYT).date().isoformat()


def day_times(now: datetime, offset: int) -> set:
    """Every valid time the 24 h means and rolling windows of that day read."""
    needed = set()
    for end in day_steps(now, offset):
        needed.update(window_steps(end))
    return needed


def forecast_offsets(series: dict, now: datetime) -> list:
    """Offsets (1 = tomorrow, up to FORECAST_DAYS) whose CAMS data is complete.

    Stops at the first incomplete day, so a short run gives fewer days and never a gap.
    """
    offsets = []
    for offset in range(1, FORECAST_DAYS + 1):
        if not day_times(now, offset) <= series.keys():
            break
        offsets.append(offset)
    return offsets


def last_forecast_time(now: datetime) -> datetime:
    """Last 3-hourly instant of day +3: the CAMS lead the full forecast needs."""
    return day_steps(now, FORECAST_DAYS)[-1]


def required_times(now: datetime) -> list:
    """Every valid time the CAMS timeline must contain for this `now`."""
    needed = set(window_steps(now_end(now)))
    for end in tomorrow_steps(now):
        needed.update(window_steps(end))
    return sorted(needed)


def pm_to_api_list(means) -> list:
    return [pm25_to_api(float(v)) for v in means]


def round_pm(value: float) -> float:
    return float(round_half_up(value, 1))


def day_range(series: dict, steps: list, to_index) -> tuple:
    """(24 h mean index, rolling minimum, rolling maximum) per cell for one day's steps.

    `to_index` maps a PM2.5 value to an integer index (API or PSI). The rolling values come
    from the trailing 24 h window ending at each step of the day.
    """
    day = [to_index(float(v)) for v in mean_of(series, steps)]
    rolling = np.array([[to_index(float(v)) for v in mean_of(series, window_steps(e))]
                        for e in steps])
    return day, rolling.min(axis=0), rolling.max(axis=0)


def cell_days(series: dict, now: datetime, offsets: list) -> list:
    """Per cell: one {date, api, api_min, api_max, band} per offset, same method as tomorrow."""
    per_day = [(offset, day_range(series, day_steps(now, offset), pm25_to_api))
               for offset in offsets]
    cells = len(per_day[0][1][0]) if per_day else 0
    return [[{"date": day_date(now, offset), "api": int(api[k]), "api_min": int(lo[k]),
              "api_max": int(hi[k]), "band": band(api[k])}
             for offset, (api, lo, hi) in per_day] for k in range(cells)]


def cell_air(series: dict, now: datetime) -> list:
    """Per cell: {'now': ..., 'tomorrow': ...} dicts of the contract."""
    pm_now = mean_of(series, window_steps(now_end(now)))
    day = tomorrow_steps(now)
    api_day = pm_to_api_list(mean_of(series, day))
    rolling = np.array([pm_to_api_list(mean_of(series, window_steps(e))) for e in day])
    out = []
    for k, pm in enumerate(pm_now):
        if math.isnan(pm):
            raise PipelineError(f"CAMS PM2.5 is missing for cell index {k}.")
        api = pm25_to_api(float(pm))
        out.append({
            "now": {"pm25_24h": round_pm(float(pm)), "api": api, "band": band(api)},
            "tomorrow": {"api": api_day[k], "api_min": int(rolling[:, k].min()),
                         "api_max": int(rolling[:, k].max()), "band": band(api_day[k])},
        })
    return out


def wind_vector(u_list: list, v_list: list) -> dict:
    """Vector-mean wind: {'from_deg', 'from_compass', 'speed_kmh'}.

    Averaging components, not speeds, so opposing winds cancel as the air actually does.
    """
    u, v = float(np.mean(u_list)), float(np.mean(v_list))
    speed = math.hypot(u, v) * MS_TO_KMH
    # Meteorological direction: where the wind comes FROM, clockwise from north.
    from_deg = math.degrees(math.atan2(-u, -v)) % 360 if speed >= CALM_KMH else 0.0
    return {"from_deg": int(round_half_up(from_deg)) % 360, "from_compass": compass(from_deg),
            "speed_kmh": int(round_half_up(speed))}


def cell_wind(wind: dict, today: list) -> list:
    """Per-cell wind from {valid: (u_cells, v_cells)} over today's MYT steps."""
    missing = [t for t in today if t not in wind]
    if missing:
        raise PipelineError("GFS wind lacks " + ", ".join(f"{t:%d %H}Z" for t in missing))
    us = np.array([wind[t][0] for t in today])
    vs = np.array([wind[t][1] for t in today])
    return [wind_vector(us[:, k], vs[:, k]) for k in range(us.shape[1])]


def today_steps(now: datetime) -> list:
    return steps_between(*myt_day_bounds(now, 0))


def earliest_needed(now: datetime) -> datetime:
    return required_times(now)[0]


def lead_hours(run: datetime, now: datetime) -> int:
    """Hours from `run` to the last required valid time."""
    return int((required_times(now)[-1] - run) / timedelta(hours=1))

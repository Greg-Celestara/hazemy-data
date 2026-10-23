"""GFS weather per cell for v2: 2 m temperature, 2 m humidity, rain, and the NOAA heat index.

The three fields come from the NOMADS grid filter, which cuts each 0.5 degree file down to the
HazeMY region on the server (about 10 KB per forecast hour instead of 750 KB from the bucket).
A "wx" bundle is that filtered GRIB, cached like the other bundles by (cycle, forecast hour).
A forecast hour that cannot be had leaves the fields that need it null; nothing is invented.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta

import numpy as np

from hazemy import compute, gfs, gfs_extra
from hazemy.common import PipelineError, STEP, http_get, myt_day_bounds
from hazemy.doe import round_half_up

NOMADS_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p50.pl"
USER_AGENT = "HazeMY-data"
# west, east, south, north: covers every HazeMY cell with a margin for bilinear sampling.
REGION = (94.0, 122.0, -4.0, 10.0)
FETCH_ATTEMPTS = 2
FETCH_TIMEOUT_S = 30
MAX_MISSES = 3  # forecast hours that may fail in a row before a cycle is given up
KELVIN = 273.15
HEAT_MIN_C = 27.0  # below this the heat index is not used: feels like equals the temperature
KIND = "wx"


def nomads_query(cycle: datetime, fhr: int) -> str:
    west, east, south, north = REGION
    return (f"{NOMADS_URL}?dir=%2Fgfs.{cycle:%Y%m%d}%2F{cycle:%H}%2Fatmos"
            f"&file=gfs.t{cycle:%H}z.pgrb2full.0p50.f{fhr:03d}"
            "&var_TMP=on&var_RH=on&var_APCP=on&lev_2_m_above_ground=on&lev_surface=on"
            f"&subregion=&leftlon={west:g}&rightlon={east:g}&toplat={north:g}&bottomlat={south:g}")


def fetch_wx(cycle: datetime, fhr: int, budget, *, log=print, **http) -> bytes:
    """The filtered GRIB of one forecast hour. `http` may carry urlopen and sleep (tests)."""
    body = http_get(nomads_query(cycle, fhr), label=f"NOMADS {cycle:%Y%m%d%H} f{fhr:03d} wx",
                    budget=budget, log=log, headers={"User-Agent": USER_AGENT},
                    attempts=FETCH_ATTEMPTS, timeout=FETCH_TIMEOUT_S, **http)
    if body[:4] != b"GRIB":
        raise PipelineError(f"NOMADS {cycle:%Y%m%d%H} f{fhr:03d}: reply is not a GRIB file.")
    return body


def heat_index_c(temp_c, rh):
    """NOAA heat index in deg C (Rothfusz regression with NOAA's adjustments), vectorised.

    Below HEAT_MIN_C the temperature itself is returned. NOAA uses the simple Steadman formula
    when its average with the temperature is under 80 F, and the regression otherwise.
    """
    t = np.asarray(temp_c, dtype=float) * 9 / 5 + 32
    r = np.clip(np.asarray(rh, dtype=float), 0.0, 100.0)
    simple = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + r * 0.094)
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * r - 0.22475541 * t * r
          - 0.00683783 * t * t - 0.05481717 * r * r + 0.00122874 * t * t * r
          + 0.00085282 * t * r * r - 0.00000199 * t * t * r * r)
    dry = (r < 13) & (t >= 80) & (t <= 112)
    hi = np.where(dry, hi - (13 - r) / 4 * np.sqrt(np.abs((17 - np.abs(t - 95)) / 17)), hi)
    humid = (r > 85) & (t >= 80) & (t <= 87)
    hi = np.where(humid, hi + (r - 85) / 10 * (87 - t) / 5, hi)
    hi = np.where((simple + t) / 2 < 80, simple, hi)
    feels = (hi - 32) * 5 / 9
    return np.where(np.asarray(temp_c, dtype=float) < HEAT_MIN_C, temp_c, feels)


def _messages(path: str) -> list:
    with open(path, "rb") as fh:
        data = fh.read()
    out, pos = [], 0
    while pos < len(data):
        if data[pos:pos + 4] != b"GRIB":
            raise PipelineError(f"{path}: not a GRIB file at byte {pos}.")
        length = int.from_bytes(data[pos + 8:pos + 16], "big")
        out.append(data[pos:pos + length])
        pos += length
    return out


def _label(raw: bytes) -> tuple:
    """(shortName, first hour of the accumulation) of one message."""
    from eccodes import codes_get, codes_new_from_message, codes_release
    handle = codes_new_from_message(raw)
    try:
        name = codes_get(handle, "shortName")
        step_range = str(codes_get(handle, "stepRange"))
        level = codes_get(handle, "typeOfLevel")
    finally:
        codes_release(handle)
    start = int(step_range.split("-")[0]) if "-" in step_range else 0
    return name, start, level


def decode_wx(path: str) -> dict:
    """{'t': (field, geometry), 'rh': ..., 'apcp': ...}: 2 m T (K), 2 m RH (%), rain since 0 h.

    The filter also returns the skin temperature and the 6 h rain buckets; they are ignored.
    """
    found = {}
    for raw in _messages(path):
        name, start, level = _label(raw)
        if name == "2t" and level == "heightAboveGround":
            found["t"] = gfs.decode_message(raw)
        elif name == "2r":
            found["rh"] = gfs.decode_message(raw)
        elif name == "tp" and start == 0:
            found.setdefault("apcp", gfs.decode_message(raw))
    return found


class WeatherFields:
    """Per forecast hour of one cycle: temperature (deg C), humidity (%) and rain since 0 h."""

    def __init__(self, cycle: datetime, n_cells: int):
        self.cycle, self.n = cycle, n_cells
        self.temp, self.rh, self.acc = {}, {}, {0: np.zeros(n_cells)}

    def add(self, fhr: int, fields: dict, lats, lons) -> None:
        def sample(key):
            field, geometry = fields[key]
            return gfs.bilinear(field, geometry, lats, lons)
        if "t" in fields and "rh" in fields:
            self.temp[fhr], self.rh[fhr] = sample("t") - KELVIN, sample("rh")
        if "apcp" in fields and fhr > 0:
            self.acc[fhr] = np.maximum(sample("apcp"), 0.0)

    def hours(self, moment: datetime) -> float:
        return (moment - self.cycle) / timedelta(hours=1)

    def accumulated(self, moment: datetime):
        """Rain since the cycle start at `moment`, linear between 3 h steps; None if missing."""
        hour = self.hours(moment)
        low = int(hour // 3) * 3
        frac = (hour - low) / 3
        if low not in self.acc or (frac > 0 and low + 3 not in self.acc):
            return None
        return self.acc[low] if frac == 0 else (
            self.acc[low] * (1 - frac) + self.acc[low + 3] * frac)

    def rain_between(self, start: datetime, end: datetime):
        a, b = self.accumulated(start), self.accumulated(end)
        return None if a is None or b is None else np.maximum(b - a, 0.0)

    def step_values(self, steps: list):
        """(temp, rh) stacks over `steps`, or None if any step is missing."""
        fhrs = [int(self.hours(t)) for t in steps]
        if not fhrs or any(f not in self.temp for f in fhrs):
            return None
        return np.array([self.temp[f] for f in fhrs]), np.array([self.rh[f] for f in fhrs])


def needed_hours(cycle: datetime, now: datetime, offsets: list) -> list:
    """Forecast hours of `cycle` that the now, today and day fields read."""
    moments = set(compute.today_steps(now)) | {nearest_step(now), nearest_step(now) - STEP}
    rain_edges = {now, myt_day_bounds(now, 0)[1]}
    for offset in offsets:
        moments |= set(compute.day_steps(now, offset))
        rain_edges |= set(myt_day_bounds(now, offset))
    fhrs = {int((t - cycle) / timedelta(hours=1)) for t in moments}
    for edge in rain_edges:
        low = math.floor((edge - cycle) / timedelta(hours=3)) * 3
        fhrs |= {low, low + 3} if edge != cycle + timedelta(hours=low) else {low}
    return sorted(f for f in fhrs if f >= 0)


def nearest_step(now: datetime) -> datetime:
    low = compute.now_end(now)
    return low if now - low <= STEP / 2 else low + STEP


def _discard(path: str) -> None:
    """Remove a cached bundle that could not be read, so the next run fetches it again."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def load_cycle(cycle: datetime, hours: list, lats, lons, cache_dir: str, budget, fetch,
               log=print):
    """WeatherFields with every hour that could be had; None if the cycle gives nothing."""
    fields = WeatherFields(cycle, len(lats))
    misses = 0
    for fhr in hours:
        try:
            path = gfs_extra.cached_bundle(cache_dir, cycle, fhr, KIND, budget, fetch, log)
            fields.add(fhr, decode_wx(path), lats, lons)
            misses = 0
        except Exception as err:  # noqa: BLE001 - one bad hour must only null its own fields
            _discard(gfs_extra.bundle_path(cache_dir, cycle, fhr, KIND))
            misses += 1
            log(f"GFS weather {cycle:%Y-%m-%d %H} UTC f{fhr:03d} unavailable: "
                f"{type(err).__name__}: {err}")
            if misses >= MAX_MISSES:
                break
    return fields if fields.temp else None


def load_weather(now: datetime, newest: datetime, offsets: list, lats, lons, cache_dir: str,
                 budget, fetch, log=print):
    """WeatherFields from the newest cycle, else the one before; None when neither works."""
    for cycle in (newest, newest - timedelta(hours=gfs.CYCLE_H)):
        hours = needed_hours(cycle, now, offsets)
        loaded = load_cycle(cycle, hours, lats, lons, cache_dir, budget, fetch, log)
        if loaded is not None:
            return loaded
        log(f"GFS weather: cycle {cycle:%Y-%m-%d %H} UTC unusable")
    return None


def _num(value, digits: int = 1):
    if value is None or not np.isfinite(value):
        return None
    return float(round_half_up(float(value), digits)) if digits else int(round_half_up(
        float(value)))


def _col(array, k: int):
    return None if array is None else float(array[k])


def _four(temp_min, temp_max, feels_max, rain, k: int) -> dict:
    return {"temp_min_c": _num(_col(temp_min, k)), "temp_max_c": _num(_col(temp_max, k)),
            "feels_max_c": _num(_col(feels_max, k), 0), "rain_mm": _num(_col(rain, k))}


def _span(stack, rain, n: int) -> list:
    """Per cell {temp_min_c, temp_max_c, feels_max_c, rain_mm} over the steps in `stack`."""
    if stack is None:
        low = high = feels = None
    else:
        temp, rh = stack
        low, high = temp.min(axis=0), temp.max(axis=0)
        feels = heat_index_c(temp, rh).max(axis=0)
    return [_four(low, high, feels, rain, k) for k in range(n)]


def now_weather(fields: WeatherFields, now: datetime) -> list:
    """Per cell {temp_c, rh, feels_c, rain_mm_3h} at the forecast step nearest to `now`."""
    step = nearest_step(now)
    stack = fields.step_values([step])
    rain = fields.rain_between(step - STEP, step)
    out = []
    for k in range(fields.n):
        if stack is None:
            out.append({"temp_c": None, "rh": None, "feels_c": None,
                        "rain_mm_3h": _num(_col(rain, k))})
            continue
        temp, rh = stack[0][0], stack[1][0]
        out.append({"temp_c": _num(temp[k]), "rh": _num(rh[k], 0),
                    "feels_c": _num(heat_index_c(temp[k], rh[k]), 0),
                    "rain_mm_3h": _num(_col(rain, k))})
    return out


def today_weather(fields: WeatherFields, now: datetime) -> list:
    """The rest of today's MYT day: from the step nearest to `now` to the end of the day."""
    start, end = myt_day_bounds(now, 0)
    steps = compute.today_steps(now)
    first = min(nearest_step(now), steps[-1])
    stack = fields.step_values([t for t in steps if t >= first])
    return _span(stack, fields.rain_between(now, end), fields.n)


def day_weather(fields: WeatherFields, now: datetime, offset: int) -> list:
    """The whole MYT day `offset` days after today's."""
    start, end = myt_day_bounds(now, offset)
    stack = fields.step_values(compute.day_steps(now, offset))
    return _span(stack, fields.rain_between(start, end), fields.n)


def cell_weather(fields, now: datetime, offsets: list, n: int) -> dict:
    """{'now': [...], 'today': [...], 'days': {offset: [...]}} with one entry per cell.

    With no usable cycle (`fields` is None) every value is null.
    """
    if fields is None:
        fields = WeatherFields(now, n)
    return {"now": now_weather(fields, now), "today": today_weather(fields, now),
            "days": {offset: day_weather(fields, now, offset) for offset in offsets}}

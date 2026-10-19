"""NOAA GFS 10 m wind from the public AWS bucket, fetched by byte range.

Only the UGRD and VGRD "10 m above ground" messages are downloaded (about 250 KB each at 0.5
degrees; the 0.25 degree grid would only quadruple the traffic for a 0.4 degree product).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from hazemy.common import PipelineError, floor_hours, http_get

BASE_URL = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
LEVEL = "10 m above ground"
VARIABLES = ("UGRD", "VGRD")
CYCLE_H = 6
# A cycle is normally complete on the bucket about 4 h after its nominal time.
PUBLISH_LAG_H = 5


@dataclass(frozen=True)
class Message:
    start: int
    end: int  # inclusive
    var: str


def latest_cycle(now: datetime) -> datetime:
    return floor_hours(now - timedelta(hours=PUBLISH_LAG_H), CYCLE_H)


def file_url(cycle: datetime, fhr: int) -> str:
    return (f"{BASE_URL}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/"
            f"gfs.t{cycle:%H}z.pgrb2.0p50.f{fhr:03d}")


def plan_steps(valid_times: list, newest: datetime) -> list:
    """[(valid, cycle, fhr)]: analysis or f003 of the valid time's own cycle when it is already
    past, otherwise the forecast of the newest cycle.

    Using each past time's own cycle keeps "today so far" made of analyses, not of one forecast.
    """
    plan = []
    for valid in valid_times:
        if valid <= newest:
            cycle = floor_hours(valid, CYCLE_H)
        else:
            cycle = newest
        fhr = int((valid - cycle).total_seconds() // 3600)
        if fhr not in (0, 3) and valid <= newest:
            raise PipelineError(f"GFS plan: {valid} is not a 3-hourly step of a cycle.")
        plan.append((valid, cycle, fhr))
    return plan


def parse_idx(text: str) -> list:
    """[(offset, var, level)] sorted by offset, from a wgrib2 .idx file."""
    entries = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 5:
            entries.append((int(parts[1]), parts[3], parts[4]))
    return sorted(entries)


def select_wind(entries: list) -> list:
    """Byte ranges of the 10 m U and V messages. Raises if either is missing or the last entry."""
    messages = []
    for var in VARIABLES:
        found = [k for k, (_, v, level) in enumerate(entries) if v == var and level == LEVEL]
        if len(found) != 1 or found[0] + 1 >= len(entries):
            raise PipelineError(f"GFS .idx: expected one usable {var} '{LEVEL}' entry, "
                                f"found {len(found)}.")
        k = found[0]
        messages.append(Message(entries[k][0], entries[k + 1][0] - 1, var))
    return messages


def fetch_wind_messages(cycle: datetime, fhr: int, budget, *, urlopen=None, sleep=None,
                        log=print) -> dict:
    """{'UGRD': bytes, 'VGRD': bytes} for one GFS file."""
    extra = {k: v for k, v in (("urlopen", urlopen), ("sleep", sleep)) if v}
    url = file_url(cycle, fhr)
    label = f"GFS {cycle:%Y%m%d%H} f{fhr:03d}"
    idx = http_get(url + ".idx", label=label + " idx", budget=budget, log=log, **extra)
    out = {}
    for msg in select_wind(parse_idx(idx.decode("ascii", "replace"))):
        body = http_get(url, label=f"{label} {msg.var}", budget=budget, log=log, ok=(206,),
                        headers={"Range": f"bytes={msg.start}-{msg.end}"}, **extra)
        if len(body) != msg.end - msg.start + 1:
            raise PipelineError(f"{label} {msg.var}: range returned {len(body)} bytes, "
                                f"expected {msg.end - msg.start + 1}.")
        out[msg.var] = body
    return out


def decode_message(raw: bytes):
    """(field (nj, ni), geometry) of one GRIB2 message."""
    from eccodes import codes_get, codes_get_values, codes_new_from_message, codes_release
    handle = codes_new_from_message(raw)
    try:
        geometry = {
            "ni": codes_get(handle, "Ni"), "nj": codes_get(handle, "Nj"),
            "lat_first": codes_get(handle, "latitudeOfFirstGridPointInDegrees"),
            "lon_first": codes_get(handle, "longitudeOfFirstGridPointInDegrees"),
            "di": codes_get(handle, "iDirectionIncrementInDegrees"),
            "dj": codes_get(handle, "jDirectionIncrementInDegrees"),
            "south_first": bool(codes_get(handle, "jScansPositively")),
        }
        values = np.asarray(codes_get_values(handle), dtype=float)
    finally:
        codes_release(handle)
    if values.size != geometry["ni"] * geometry["nj"]:
        raise PipelineError("GFS message size does not match its grid.")
    return values.reshape(geometry["nj"], geometry["ni"]), geometry


def bilinear(field, geometry: dict, lats, lons):
    """Bilinear sample of `field` at points; raises if a point is outside the grid."""
    sign = 1 if geometry["south_first"] else -1
    rows = (lats - geometry["lat_first"]) / (sign * geometry["dj"])
    cols = (lons - geometry["lon_first"]) / geometry["di"]
    if (rows.min() < 0 or cols.min() < 0 or rows.max() > geometry["nj"] - 1
            or cols.max() > geometry["ni"] - 1):
        raise PipelineError("GFS grid does not cover every cell.")
    r0 = np.minimum(np.floor(rows).astype(int), geometry["nj"] - 2)
    c0 = np.minimum(np.floor(cols).astype(int), geometry["ni"] - 2)
    fr, fc = rows - r0, cols - c0
    top = field[r0, c0] * (1 - fc) + field[r0, c0 + 1] * fc
    bottom = field[r0 + 1, c0] * (1 - fc) + field[r0 + 1, c0 + 1] * fc
    return top * (1 - fr) + bottom * fr


def collect_wind(valid_times: list, now: datetime, lats, lons, budget, *, log=print,
                 fetch=fetch_wind_messages) -> tuple:
    """({valid: (u_cells, v_cells)}, newest_cycle_used).

    If the newest cycle is not on the bucket yet, the previous one is used.
    """
    last_error = None
    for newest in (latest_cycle(now), latest_cycle(now) - timedelta(hours=CYCLE_H)):
        try:
            return _collect(valid_times, newest, lats, lons, budget, log, fetch), newest
        except PipelineError as err:
            last_error = err
            log(f"GFS cycle {newest:%Y-%m-%d %H} UTC unusable: {err}")
    raise PipelineError(f"No usable GFS cycle: {last_error}")


def _collect(valid_times, newest, lats, lons, budget, log, fetch) -> dict:
    fetched, result = {}, {}
    for valid, cycle, fhr in plan_steps(valid_times, newest):
        if (cycle, fhr) not in fetched:
            fetched[(cycle, fhr)] = fetch(cycle, fhr, budget, log=log)
        raw = fetched[(cycle, fhr)]
        u, geometry = decode_message(raw["UGRD"])
        v, _ = decode_message(raw["VGRD"])
        result[valid] = (bilinear(u, geometry, lats, lons), bilinear(v, geometry, lats, lons))
    return result

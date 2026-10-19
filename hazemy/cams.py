"""CAMS global PM2.5 forecast: ADS request, cached download, GRIB decode, cell sampling."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np

from hazemy.common import PipelineError, floor_hours

ADS_URL = "https://ads.atmosphere.copernicus.eu/api"
DATASET = "cams-global-atmospheric-composition-forecasts"
# north, west, south, east: a margin of one cell around the HazeMY grid keeps the download small.
AREA = [8.0, 99.2, 0.4, 120.0]
KG_M3_TO_UG_M3 = 1e9
# The 00 UTC run is published about 10:00 UTC.
PUBLISH_LAG_H = 10
COORD_TOLERANCE_DEG = 1e-3


def expected_run(now: datetime) -> datetime:
    """The 00 UTC run that should be published at `now`."""
    return floor_hours(now - timedelta(hours=PUBLISH_LAG_H), 24)


def build_request(run: datetime, max_lead_h: int) -> dict:
    leads = list(range(0, max_lead_h + 1, 3))
    return {
        "variable": ["particulate_matter_2.5um"],
        "date": [f"{run:%Y-%m-%d}/{run:%Y-%m-%d}"],
        "time": ["00:00"],
        "leadtime_hour": [str(h) for h in leads],
        "type": ["forecast"],
        "data_format": "grib",
        "area": AREA,
    }


def retrieve_grib(run: datetime, max_lead_h: int, target: str, key: str) -> None:
    """Download one run with cdsapi (imported here so unit tests need not have it)."""
    import cdsapi
    client = cdsapi.Client(url=ADS_URL, key=key, quiet=True, progress=False)
    client.retrieve(DATASET, build_request(run, max_lead_h), target)


def cached_grib(run: datetime, max_lead_h: int, cache_dir: str, key: str, budget,
                retrieve=retrieve_grib, log=print) -> str:
    """Path of the run's GRIB; downloaded only if the cache lacks it.

    The cache name carries the lead span so a different request never reuses another file; callers
    pass a fixed span so the name is stable for the whole run day. The
    download goes to a temporary name first so an interrupted run leaves no half file.
    """
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"cams_{run:%Y%m%d}_00z_lt{max_lead_h}.grib")
    if os.path.exists(path):
        log(f"using cached {os.path.basename(path)}")
        return path
    log(f"requesting CAMS run {run:%Y-%m-%d} 00 UTC, lead 0-{max_lead_h} h")
    partial = path + ".part"
    retrieve(run, max_lead_h, partial, key)
    size = os.path.getsize(partial)
    budget.add(size)
    os.replace(partial, path)
    log(f"downloaded {size} bytes")
    return path


def decode_grib(path: str) -> dict:
    """Every message: {valid_utc: (field (nj, ni) in ug/m3, geometry dict)}.

    A field repeated for the same valid time is an error. Missing values become NaN.
    """
    from eccodes import (codes_get, codes_get_values, codes_grib_new_from_file,
                         codes_release)
    fields = {}
    with open(path, "rb") as fh:
        while True:
            handle = codes_grib_new_from_file(fh)
            if handle is None:
                break
            try:
                if codes_get(handle, "shortName") != "pm2p5":
                    raise PipelineError(
                        f"{path}: unexpected GRIB parameter {codes_get(handle, 'shortName')!r}.")
                geometry = _geometry(handle, codes_get)
                valid = _valid_time(handle, codes_get)
                values = np.asarray(codes_get_values(handle), dtype=float)
                values[values >= codes_get(handle, "missingValue")] = np.nan
                if valid in fields:
                    raise PipelineError(f"{path}: duplicate message for {valid:%Y-%m-%d %H} UTC.")
                shaped = values.reshape(geometry["nj"], geometry["ni"])
                fields[valid] = (shaped * KG_M3_TO_UG_M3, geometry)
            finally:
                codes_release(handle)
    if not fields:
        raise PipelineError(f"{path}: the GRIB file contains no messages.")
    return fields


def _geometry(handle, get) -> dict:
    geometry = {
        "ni": get(handle, "Ni"), "nj": get(handle, "Nj"),
        "lat_first": get(handle, "latitudeOfFirstGridPointInDegrees"),
        "lon_first": get(handle, "longitudeOfFirstGridPointInDegrees"),
        "di": get(handle, "iDirectionIncrementInDegrees"),
        "dj": get(handle, "jDirectionIncrementInDegrees"),
        "south_first": bool(get(handle, "jScansPositively")),
    }
    return geometry


def _valid_time(handle, get) -> datetime:
    date, hhmm, step = get(handle, "dataDate"), get(handle, "dataTime"), get(handle, "step")
    run = datetime(date // 10000, date // 100 % 100, date % 100, hhmm // 100, hhmm % 100,
                   tzinfo=timezone.utc)
    return run + timedelta(hours=int(step))


def sample_cells(field, geometry: dict, lats, lons):
    """Values of `field` at the cell centres; the CAMS 0.4 degree points coincide with them.

    Raises if a centre is not on a grid point, because an interpolated value would silently
    differ from what the contract promises.
    """
    sign = 1 if geometry["south_first"] else -1
    rows = (lats - geometry["lat_first"]) / (sign * geometry["dj"])
    cols = (lons - geometry["lon_first"]) / geometry["di"]
    r, c = np.rint(rows).astype(int), np.rint(cols).astype(int)
    off = max(np.abs(rows - r).max(), np.abs(cols - c).max()) * geometry["di"]
    if off > COORD_TOLERANCE_DEG:
        raise PipelineError(f"CAMS grid does not line up with the cell centres (off {off} deg).")
    if r.min() < 0 or c.min() < 0 or r.max() >= geometry["nj"] or c.max() >= geometry["ni"]:
        raise PipelineError("CAMS download does not cover every cell; check AREA in cams.py.")
    return field[r, c]


def merge_runs(runs: list) -> dict:
    """Merge decoded runs, oldest first; where valid times overlap the newer run wins."""
    merged = {}
    for decoded in runs:
        merged.update(decoded)
    return merged

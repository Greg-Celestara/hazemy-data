"""Singapore additions: land outline, NEA PSI conversion of CAMS PM2.5, NEA real-time PSI."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import numpy as np
import shapely
from shapely.geometry import Polygon

from hazemy import compute
from hazemy.common import PipelineError, fmt, http_get
from hazemy.doe import band, round_half_up
from hazemy.grid import DATA_DIR, KM_PER_DEG

SG_LAND_PATH = os.path.join(DATA_DIR, "singapore.json")
TOWNS_PATH = os.path.join(DATA_DIR, "towns_sg.json")
SG_RADIUS_KM = 30.0
NEA_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"
NEA_REGIONS = ("north", "south", "east", "west", "central")
NEA_ATTRIBUTION = ("Contains information from PSI accessed on {date} from data.gov.sg which is "
                   "made available under the terms of the Singapore Open Data Licence version "
                   "1.0 https://data.gov.sg/open-data-licence")
# 24-hr PM2.5 sub-index from NEA, "Computation of the Pollutant Standards Index (PSI)", last
# updated March 2014:
# http://www.haze.gov.sg/docs/default-source/faq/computation-of-the-pollutant-standards-index-(psi).pdf
# Linked from https://www.nea.gov.sg/our-services/pollution-control/air-pollution/faqs
# The concentration is rounded to an integer, then mapped linearly within its segment:
# (first conc, last conc, first PSI, last PSI). Above 500 ug/m3 the index is capped at 500.
# The document's worked example (40 ug/m3 -> 83) is a test. The Android app (Psi.kt) uses the
# same method and must stay identical.
PSI_SEGMENTS = ((0, 12, 0, 50), (13, 55, 51, 100), (56, 150, 101, 200), (151, 250, 201, 300),
                (251, 350, 301, 400), (351, 500, 401, 500))
PSI_CAP = 500


def load_land_km():
    """Singapore's land in km units (x east, y north), for distance checks."""
    with open(SG_LAND_PATH) as fh:
        ring = json.load(fh)["ring"]
    return shapely.transform(Polygon(ring), lambda xy: xy * KM_PER_DEG)


def load_towns(path: str = TOWNS_PATH) -> list:
    with open(path) as fh:
        return json.load(fh)["towns"]


def pm25_to_psi(pm25: float) -> int:
    """NEA PM2.5 sub-index of a 24 h mean concentration, as an integer (half up)."""
    conc = int(round_half_up(max(float(pm25), 0.0)))
    if conc > PSI_SEGMENTS[-1][1]:
        return PSI_CAP
    for c0, c1, i0, i1 in PSI_SEGMENTS:
        if conc <= c1:
            # Integer arithmetic, so exact halves round up as in the NEA example.
            span = c1 - c0
            return (2 * (i0 * span + (conc - c0) * (i1 - i0)) + span) // (2 * span)
    return PSI_CAP


def sg_flags(cells: list, land_km, radius_km: float = SG_RADIUS_KM) -> list:
    """True for each (i, j, lat, lon) cell whose centre is within `radius_km` of Singapore."""
    return [land_km.distance(shapely.Point(lon * KM_PER_DEG, lat * KM_PER_DEG)) <= radius_km
            for _, _, lat, lon in cells]


def cell_sg(series: dict, now: datetime) -> list:
    """Per cell: the `sg` dict (tomorrow PSI from the same 24 h means as `tomorrow`)."""
    day = compute.tomorrow_steps(now)
    psi_day = [pm25_to_psi(v) for v in compute.mean_of(series, day)]
    rolling = np.array([[pm25_to_psi(v) for v in compute.mean_of(series, compute.window_steps(e))]
                        for e in day])
    return [{"tomorrow_psi": psi_day[k], "tomorrow_psi_min": int(rolling[:, k].min()),
             "tomorrow_psi_max": int(rolling[:, k].max()), "band": band(psi_day[k])}
            for k in range(len(psi_day))]


def parse_nea(text: str) -> dict:
    """{'updated_utc', 'regions'} from the data.gov.sg v2 PSI response; raises if incomplete."""
    try:
        data = json.loads(text)["data"]
        meta = {m["name"]: m["labelLocation"] for m in data["regionMetadata"]}
        item = max(data["items"], key=lambda it: it["timestamp"])
        readings = item["readings"]["psi_twenty_four_hourly"]
        stamp = datetime.fromisoformat(item["timestamp"]).astimezone(timezone.utc)
        regions = {}
        for name in NEA_REGIONS:
            psi = readings[name]
            if isinstance(psi, bool) or not isinstance(psi, int) or not 0 <= psi <= 1000:
                raise ValueError(f"{name} PSI {psi!r}")
            regions[name] = {"lat": float(meta[name]["latitude"]),
                             "lon": float(meta[name]["longitude"]),
                             "psi_24h": psi, "band": band(psi)}
    except (KeyError, TypeError, ValueError, IndexError) as err:
        raise PipelineError(
            f"NEA PSI response not understood: {type(err).__name__}: {err}") from err
    return {"updated_utc": fmt(stamp), "regions": regions}


def fetch_nea(now: datetime, budget, *, log=print, **http):
    """The `nea` block, or None if NEA is unreachable or answers something unusable.

    NEA is an optional extra: the app falls back to the model PSI, so a failure here must not
    stop the job.
    """
    try:
        body = http_get(NEA_URL, label="NEA PSI", budget=budget, log=log, **http)
        return parse_nea(body.decode("utf-8", "replace"))
    except PipelineError as err:
        log(f"NEA PSI unavailable, omitting 'nea': {err}")
        return None


def attribution(now: datetime) -> str:
    return NEA_ATTRIBUTION.format(date=f"{now.day} {now:%b %Y}")

"""Assemble hazemy.json, validate it against the contract (docs/schema.md) and write it."""
from __future__ import annotations

import json
import os

from hazemy.common import PipelineError, fmt
from hazemy.grid import GRID

MAX_BYTES = 500_000
COMPASS_POINTS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
BASE_ATTRIBUTION = "Contains modified Copernicus Atmosphere Monitoring Service information {year}"
ATTRIBUTION_TAIL = "; NASA FIRMS; NOAA GFS"


def assemble(*, generated, cams_run, firms_fetched, gfs_run, cells, air, wind, fires, towns,
             firms_updated=None):
    """The document dict; `cells` are (i, j, lat, lon), air and wind are parallel lists."""
    records = []
    for (i, j, _, _), a, w in zip(cells, air, wind):
        records.append({"i": i, "j": j, "now": a["now"], "tomorrow": a["tomorrow"],
                        "wind_today": w})
    attribution = BASE_ATTRIBUTION.format(year=cams_run.year) + ATTRIBUTION_TAIL
    return {
        "version": 1,
        "generated_utc": fmt(generated),
        "sources": {"cams_run_utc": fmt(cams_run), "firms_fetched_utc": fmt(firms_fetched),
                    "firms_updated_utc": fmt(firms_updated) if firms_updated else None,
                    "gfs_run_utc": fmt(gfs_run), "attribution": attribution},
        "grid": dict(GRID),
        "cells": records,
        "fires_72h": fires,
        "towns": towns,
    }


def _need(cond: bool, message: str) -> None:
    if not cond:
        raise PipelineError(f"Output invalid: {message}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_cell(cell: dict) -> None:
    where = f"cell ({cell.get('i')}, {cell.get('j')})"
    _need(0 <= cell["i"] < GRID["nlat"] and 0 <= cell["j"] < GRID["nlon"], f"{where} off grid")
    now, tom, wind = cell["now"], cell["tomorrow"], cell["wind_today"]
    _need(isinstance(now["pm25_24h"], float) and now["pm25_24h"] >= 0, f"{where} pm25_24h")
    for name, value in (("now.api", now["api"]), ("tomorrow.api", tom["api"]),
                        ("api_min", tom["api_min"]), ("api_max", tom["api_max"])):
        _need(_is_int(value) and 0 <= value <= 500, f"{where} {name}={value!r}")
    _need(tom["api_min"] <= tom["api_max"], f"{where} api_min > api_max")
    _need(now["band"] in range(1, 6) and tom["band"] in range(1, 6), f"{where} band")
    _need(_is_int(wind["from_deg"]) and 0 <= wind["from_deg"] < 360, f"{where} from_deg")
    _need(wind["from_compass"] in COMPASS_POINTS, f"{where} from_compass")
    _need(_is_int(wind["speed_kmh"]) and wind["speed_kmh"] >= 0, f"{where} speed_kmh")


def validate(doc: dict) -> bytes:
    """Check shape and ranges, return the compact JSON bytes (under MAX_BYTES)."""
    expected = ["version", "generated_utc", "sources", "grid", "cells", "fires_72h", "towns"]
    _need(list(doc) == expected, f"top-level keys {list(doc)}")
    _need(doc["version"] == 1 and doc["grid"] == GRID, "version or grid")
    _need(set(doc["sources"]) == {"cams_run_utc", "firms_fetched_utc", "firms_updated_utc",
                                  "gfs_run_utc", "attribution"}, "sources keys")
    _need(len(doc["cells"]) > 0, "no cells")
    seen = set()
    for cell in doc["cells"]:
        _check_cell(cell)
        _need((cell["i"], cell["j"]) not in seen, f"duplicate cell {cell['i']},{cell['j']}")
        seen.add((cell["i"], cell["j"]))
    fires = doc["fires_72h"]
    _need(len(fires) <= 5 and all(f["count"] > 0 for f in fires), "fires_72h")
    _need([f["count"] for f in fires] == sorted((f["count"] for f in fires), reverse=True),
          "fires_72h not sorted descending")
    _need(len(doc["towns"]) > 0 and all(-90 <= t["lat"] <= 90 for t in doc["towns"]), "towns")
    body = json.dumps(doc, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()
    _need(len(body) < MAX_BYTES, f"{len(body)} bytes is not under {MAX_BYTES}")
    return body


def write_atomic(path: str, body: bytes) -> None:
    """Write via a temporary file so a crash never leaves a half-written published file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, path)

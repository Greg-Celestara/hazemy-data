"""The fixed output grid and the choice of which cells to publish."""
from __future__ import annotations

import json
import os

import numpy as np
import shapely
from shapely.geometry import shape
from shapely.ops import unary_union

from hazemy.common import PipelineError

GRID = {"lat0": 0.8, "lon0": 99.6, "step": 0.4, "nlat": 17, "nlon": 50}
# Cells whose centre lies farther than this from Malaysian land are not published.
LAND_RADIUS_KM = 50.0
KM_PER_DEG = 111.0  # one scale for both axes is accurate to <1% across lat 0-8 N
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
PROVINCES_PATH = os.path.join(DATA_DIR, "provinces.geojson")
BBOX_PAD_DEG = 0.4


def cell_center(i: int, j: int):
    """(lat, lon) of cell (i, j); rounded to kill float drift from the 0.4 step."""
    return (round(GRID["lat0"] + i * GRID["step"], 4), round(GRID["lon0"] + j * GRID["step"], 4))


def load_provinces(path: str = PROVINCES_PATH) -> list:
    """[(name, country_iso3, shapely geometry)] from the committed admin-1 file."""
    with open(path) as fh:
        collection = json.load(fh)
    provinces = [(f["properties"]["name"], f["properties"]["country"], shape(f["geometry"]))
                 for f in collection["features"]]
    if not provinces:
        raise PipelineError(f"{path} contains no provinces.")
    return provinces


def malaysia_land(provinces: list):
    """Malaysia's land in km units (x east, y north), for distance checks."""
    parts = [geom for _, country, geom in provinces if country == "MYS"]
    if not parts:
        raise PipelineError("No Malaysian provinces in the province file.")
    land = unary_union(parts)
    return shapely.transform(land, lambda xy: xy * KM_PER_DEG)


def land_cells(land_km, radius_km: float = LAND_RADIUS_KM, grid: dict = GRID) -> list:
    """[(i, j, lat, lon)] for cells whose centre is within `radius_km` of `land_km`."""
    cells = []
    for i in range(grid["nlat"]):
        for j in range(grid["nlon"]):
            lat, lon = cell_center(i, j)
            point = shapely.Point(lon * KM_PER_DEG, lat * KM_PER_DEG)
            if land_km.distance(point) <= radius_km:
                cells.append((i, j, lat, lon))
    return cells


def cell_arrays(cells: list):
    lats = np.array([c[2] for c in cells])
    lons = np.array([c[3] for c in cells])
    return lats, lons

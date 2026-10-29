"""v2/flood-spots.json: the 10 Kuala Lumpur flood hotspots DBKL named publicly (The Star, 13 May 2026).

A curated list, not a measurement: data/flood_spots.json holds the hand-geocoded pins (OSM,
(c) OpenStreetMap contributors, ODbL). `district` is the state-level name the app matches
against MetMalaysia warnings; the warning parser (hazemy/met.py) calls it "Kuala Lumpur".
Run: python -m hazemy.flood_spots --out site/v2/flood-spots.json
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib

from hazemy.common import PipelineError
from hazemy.output import write_atomic

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "data" / "flood_spots.json"
FIELDS = ("id", "name", "lat", "lon", "district", "source", "source_date")
COUNT = 10
MAX_BYTES = 10_000
KL_BBOX = (3.03, 101.58, 3.25, 101.78)  # lat_min, lon_min, lat_max, lon_max
MAX_ROAD_DISTANCE_M = 50.0
M_PER_DEG = 111_320.0


def point_to_ways_m(lat: float, lon: float, ways: list) -> float:
    """Metres from a point to the nearest segment of any way ([[lat, lon], ...])."""
    cos_lat = math.cos(math.radians(lat))
    best = math.inf
    for way in ways:
        for (la, lo), (lb, ob) in zip(way, way[1:]):
            ax, ay = (lo - lon) * cos_lat, la - lat
            bx, by = (ob - lon) * cos_lat, lb - lat
            dx, dy = bx - ax, by - ay
            seg = dx * dx + dy * dy
            t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy) * M_PER_DEG)
    return best


def validate(spots) -> bytes:
    """The compact JSON body, or PipelineError naming the first problem."""
    if not isinstance(spots, list) or len(spots) != COUNT:
        raise PipelineError(f"flood-spots: expected a list of {COUNT} spots, got "
                            f"{len(spots) if isinstance(spots, list) else type(spots).__name__}.")
    seen = set()
    for spot in spots:
        if tuple(spot) != FIELDS:
            raise PipelineError(f"flood-spots: fields {tuple(spot)} differ from {FIELDS}.")
        if spot["id"] in seen:
            raise PipelineError(f"flood-spots: duplicate id {spot['id']!r}.")
        seen.add(spot["id"])
        lat, lon = spot["lat"], spot["lon"]
        if not (KL_BBOX[0] <= lat <= KL_BBOX[2] and KL_BBOX[1] <= lon <= KL_BBOX[3]):
            raise PipelineError(f"flood-spots: {spot['id']} ({lat}, {lon}) is outside Kuala Lumpur.")
    body = json.dumps(spots, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) >= MAX_BYTES:
        raise PipelineError(f"flood-spots: {len(body)} bytes, limit {MAX_BYTES}.")
    return body


def build(out: str, source: pathlib.Path = SOURCE) -> int:
    body = validate(json.loads(source.read_text(encoding="utf-8")))
    write_atomic(out, body)
    return len(body)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m hazemy.flood_spots", description=__doc__)
    parser.add_argument("--out", default="site/v2/flood-spots.json")
    args = parser.parse_args()
    print(f"wrote {args.out} ({build(args.out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

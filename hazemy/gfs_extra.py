"""GFS 850 hPa wind and APCP rain for HazeMY v2: byte-range fetch, per-cycle cache, cropped decode.

A "bundle" is the few messages of one GFS file that v2 needs, concatenated in a fixed order:
kind "w850" = UGRD, VGRD at 850 mb; kind "rain" = the "0-N hour acc" APCP message. Each
(cycle, forecast hour, kind) is downloaded once and kept in the cache directory.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import numpy as np

from hazemy.common import PipelineError, http_get
from hazemy.gfs import CYCLE_H, decode_message, file_url

LEVEL_850 = "850 mb"
# west, east, south, north: the 0.5 degree global field is cut to this to keep memory small. It
# leaves a wide margin around the trajectory box (90-125 E, 12 S-14 N) and the HazeMY grid.
CROP = (86.0, 129.0, -16.0, 18.0)
# Cached cycles older than this are deleted; 9 cycles of trajectory data span 48 h plus margin.
KEEP_H = 9 * CYCLE_H + 6


def parse_idx_tail(text: str) -> list:
    """[(offset, var, level, tail)] sorted by offset, tail being e.g. '0-6 hour acc fcst'."""
    entries = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 6:
            entries.append((int(parts[1]), parts[3], parts[4], parts[5]))
    return sorted(entries)


def _wanted(kind: str, fhr: int) -> list:
    """[(var, level, tail or None)] in bundle order."""
    if kind == "w850":
        return [("UGRD", LEVEL_850, None), ("VGRD", LEVEL_850, None)]
    if kind == "rain":
        return [("APCP", "surface", f"0-{fhr} hour acc fcst")]
    raise ValueError(f"unknown GFS bundle kind {kind!r}")


def select_ranges(entries: list, kind: str, fhr: int) -> list:
    """[(start, end_inclusive, label)] byte ranges for the bundle.

    GFS lists APCP twice with the same label at some hours; the first one is taken.
    """
    ranges = []
    for var, level, tail in _wanted(kind, fhr):
        found = [k for k, e in enumerate(entries)
                 if e[1] == var and e[2] == level and (tail is None or e[3] == tail)]
        if not found or found[0] + 1 >= len(entries):
            raise PipelineError(f"GFS .idx: no usable {var} '{level}' {tail or ''} entry.")
        k = found[0]
        ranges.append((entries[k][0], entries[k + 1][0] - 1, var))
    return ranges


def fetch_bundle(cycle: datetime, fhr: int, kind: str, budget, *, urlopen=None, sleep=None,
                 log=print) -> bytes:
    """The bundle bytes of one GFS file, fetched message by message with byte ranges."""
    extra = {k: v for k, v in (("urlopen", urlopen), ("sleep", sleep)) if v}
    url = file_url(cycle, fhr)
    label = f"GFS {cycle:%Y%m%d%H} f{fhr:03d} {kind}"
    idx = http_get(url + ".idx", label=label + " idx", budget=budget, log=log, **extra)
    out = []
    for start, end, var in select_ranges(parse_idx_tail(idx.decode("ascii", "replace")),
                                         kind, fhr):
        body = http_get(url, label=f"{label} {var}", budget=budget, log=log, ok=(200, 206),
                        headers={"Range": f"bytes={start}-{end}"}, **extra)
        # A 200 is accepted only when it is exactly the range asked for (a server that ignored
        # the Range header sends the whole file, which fails this check).
        if len(body) != end - start + 1:
            raise PipelineError(f"{label} {var}: range returned {len(body)} bytes, "
                                f"expected {end - start + 1}.")
        out.append(body)
    return b"".join(out)


def bundle_path(cache_dir: str, cycle: datetime, fhr: int, kind: str) -> str:
    return os.path.join(cache_dir, f"gfs_{cycle:%Y%m%d%H}_f{fhr:03d}_{kind}.grib")


def cached_bundle(cache_dir: str, cycle: datetime, fhr: int, kind: str, budget, fetch,
                  log=print) -> str:
    """Path of the bundle file; downloaded only when the cache lacks it (via a .part file)."""
    os.makedirs(cache_dir, exist_ok=True)
    path = bundle_path(cache_dir, cycle, fhr, kind)
    if os.path.exists(path):
        return path
    body = fetch(cycle, fhr, kind, budget, log=log)
    partial = path + ".part"
    with open(partial, "wb") as fh:
        fh.write(body)
    os.replace(partial, path)
    return path


def prune_cache(cache_dir: str, newest: datetime) -> None:
    """Delete cached bundles whose cycle is older than KEEP_H before `newest`."""
    if not os.path.isdir(cache_dir):
        return
    oldest = newest - timedelta(hours=KEEP_H)
    for name in os.listdir(cache_dir):
        if name.startswith("gfs_") and name.endswith(".grib"):
            try:
                cycle = datetime.strptime(name[4:14], "%Y%m%d%H")
            except ValueError:
                continue
            if cycle.replace(tzinfo=newest.tzinfo) < oldest:
                os.remove(os.path.join(cache_dir, name))


def crop_field(field, geometry: dict, crop=CROP):
    """(field, geometry) restricted to the crop box; raises if the grid misses it entirely."""
    west, east, south, north = crop
    sign = 1 if geometry["south_first"] else -1
    lats = geometry["lat_first"] + sign * geometry["dj"] * np.arange(geometry["nj"])
    lons = geometry["lon_first"] + geometry["di"] * np.arange(geometry["ni"])
    rows = np.where((lats >= south) & (lats <= north))[0]
    cols = np.where((lons >= west) & (lons <= east))[0]
    if rows.size < 2 or cols.size < 2:
        raise PipelineError("GFS grid does not overlap the HazeMY region.")
    cut = dict(geometry, nj=int(rows.size), ni=int(cols.size),
               lat_first=float(lats[rows[0]]), lon_first=float(lons[cols[0]]))
    return field[np.ix_(rows, cols)], cut


def decode_bundle(path: str, expected: int) -> list:
    """[(field, geometry)] of every message in the bundle file, cropped; count must match."""
    with open(path, "rb") as fh:
        data = fh.read()
    messages, pos = [], 0
    while pos < len(data):
        if data[pos:pos + 4] != b"GRIB":
            raise PipelineError(f"{path}: not a GRIB file at byte {pos}.")
        length = int.from_bytes(data[pos + 8:pos + 16], "big")
        messages.append(data[pos:pos + length])
        pos += length
    if len(messages) != expected:
        raise PipelineError(f"{path}: {len(messages)} messages, expected {expected}.")
    out = []
    for raw in messages:
        field, geometry = decode_message(raw)
        out.append(crop_field(field, geometry))
    return out

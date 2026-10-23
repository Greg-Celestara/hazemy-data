"""One-off: write data/met_places.json, the coordinates of MetMalaysia's town and district places.

The MetMalaysia forecast feed (api.data.gov.my/weather/forecast/) lists places by id and name
only. Coordinates come from the GeoNames Malaysia dump (CC BY 4.0), matched on name and alternate
names. Only Tn (town) and Ds (district) places are written, because only those are used.

Usage: python scripts/build_met_places.py MY.txt [forecast.json]
  MY.txt        unzipped from https://download.geonames.org/export/dump/MY.zip
  forecast.json a saved copy of the feed (default: fetch it)
Prints the places with several candidates or no match: check those by hand before committing.
"""
import json
import os
import sys
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "met_places.json")
FEED = "https://api.data.gov.my/weather/forecast/"
AGENT = {"User-Agent": "HazeMY-data"}
# MetMalaysia location types used. Populated places (P) are tried before administrative areas
# (A), also for districts: an area's GeoNames point is its centroid, up to 75 km from the town
# the forecast is about (Marudi), while the district's main town is a P of the same name.
TYPES = ("Tn", "Ds")
CLASS_ORDER = "PA"
# Checked by hand against the town list: Penampang has two unpopulated same-name places in
# GeoNames, 60 km apart, and the choice between them is a coin toss. Better none than wrong.
EXCLUDE = {"Ds550"}


def places(rows: list) -> dict:
    found = {}
    for row in rows:
        loc = row["location"]
        if loc["location_id"][:2] in TYPES:
            found[loc["location_id"]] = loc["location_name"]
    return dict(sorted(found.items()))


def read_geonames(path: str) -> list:
    """Populated places and administrative areas, as dicts."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            col = line.rstrip("\n").split("\t")
            if col[6] not in ("P", "A"):
                continue
            rows.append({"names": {n.casefold() for n in [col[1], col[2]] if n},
                         "alternates": {n.casefold() for n in col[3].split(",") if n},
                         "cls": col[6], "population": int(col[14] or 0),
                         "lat": float(col[4]), "lon": float(col[5]), "admin1": col[10],
                         "geonameid": col[0]})
    return rows


def candidates(rows: list, name: str) -> list:
    """Rows whose own name matches; only when none does, rows with it as an alternate name."""
    key = name.casefold()
    own = [r for r in rows if key in r["names"]]
    return own or [r for r in rows if key in r["alternates"]]


def choose(found: list) -> dict:
    return min(found, key=lambda r: (CLASS_ORDER.index(r["cls"]), -r["population"]))


def main() -> None:
    rows = read_geonames(sys.argv[1])
    feed = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else json.load(
        urllib.request.urlopen(urllib.request.Request(FEED, headers=AGENT), timeout=60))
    out, review = [], []
    for place_id, name in places(feed).items():
        if place_id in EXCLUDE:
            continue
        found = candidates(rows, name)
        if not found:
            review.append(f"{place_id} {name}: no match")
            continue
        best = choose(found)
        if len(found) > 1:
            review.append(f"{place_id} {name}: {len(found)} candidates, took geonameid "
                          f"{best['geonameid']} (class {best['cls']}, admin1 {best['admin1']})")
        out.append({"id": place_id, "name": name, "lat": round(best["lat"], 4),
                    "lon": round(best["lon"], 4)})
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"source": "GeoNames (CC BY 4.0)", "places": out}, fh, ensure_ascii=False,
                  indent=0)
    print(f"wrote {len(out)} places to {OUT}; {len(places(feed)) - len(out)} left out")
    print("\n".join(review))


if __name__ == "__main__":
    main()

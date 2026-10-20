# hazemy.json — public data contract (v2)

Served at `https://greg-celestara.github.io/hazemy-data/v2/hazemy.json`. `v1/hazemy.json` keeps
being published unchanged (schema.md) for installed v1 apps. Same job, same hourly run.
Nothing in it identifies a user. Model values are **model estimates**; Singapore `nea` values are
**measured** by NEA.

v2 = every v1 field (same meaning, same rules as schema.md) plus the additions below.
`"version": 2`.

```json
{
  "version": 2,
  "generated_utc": "...", "sources": { "...v1 fields...": "",
    "gfs_traj_cycles_utc": ["2026-10-18T00:00:00Z", "..."],
    "nea_updated_utc": "2026-10-20T03:00:00Z",
    "nea_attribution": "Contains information from PSI accessed on 20 Oct 2026 from data.gov.sg which is made available under the terms of the Singapore Open Data Licence version 1.0 https://data.gov.sg/open-data-licence" },
  "grid": { "...": "v1" },
  "cells": [
    { "i": 6, "j": 5,
      "now": { "pm25_24h": 69.1, "api": 158, "band": 3 },
      "tomorrow": { "api": 121, "api_min": 121, "api_max": 155, "band": 3 },
      "wind_today": { "from_deg": 162, "from_compass": "S", "speed_kmh": 2 },
      "sg": { "tomorrow_psi": 118, "tomorrow_psi_min": 110, "tomorrow_psi_max": 140, "band": 3 },
      "drivers": {
        "rain_tonight_mm": 6.2,
        "wind850_today": { "from_deg": 200, "speed_kmh": 18 },
        "wind850_tomorrow": { "from_deg": 205, "speed_kmh": 26 },
        "path_fire_province": "Central Kalimantan",
        "path_fire_count_72h": 412,
        "path_fire_trend": "rising"
      },
      "path48h": [[3.14, 101.69], [3.0, 101.9], "... 17 points, 3-hourly, newest first"]
    }
  ],
  "fires_72h": [ "...v1..." ],
  "fires_daily": [ { "province": "Central Kalimantan", "country": "ID", "counts": [6100, 7800, 9308] } ],
  "fire_bins": [ [-2.125, 113.875, 412] ],
  "nea": { "updated_utc": "...", "regions": {
      "north":   { "lat": 1.41, "lon": 103.82, "psi_24h": 64, "band": 2 },
      "south": {}, "east": {}, "west": {}, "central": {} } },
  "towns": [ { "name": "Petaling Jaya", "lat": 3.107, "lon": 101.606, "country": "MY" },
             { "name": "Tampines", "lat": 1.353, "lon": 103.945, "country": "SG", "nea_region": "east" } ]
}
```

## Rules for the additions

- **Coverage.** Cells are listed if their centre is within ~50 km of Malaysian **or Singapore**
  land. Grid unchanged.
- **`towns`.** Every town gets `country` ("MY" or "SG"). Add ~20 Singapore planning areas
  (hand-compiled, centroid coordinates, each with `nea_region` = north/south/east/west/central,
  following NEA's region split). Malaysian towns unchanged.
- **`nea`.** From `https://api-open.data.gov.sg/v2/real-time/api/psi` (no key), latest item:
  `psi_twenty_four_hourly` per region, plus `regionMetadata` lat/lon. `band` by PSI range:
  1 0–50, 2 51–100, 3 101–200, 4 201–300, 5 >300. If NEA fails, omit `nea` (the app then shows
  the model PSI for Singapore and says so). Attribution string exactly as above, date = access date.
- **`sg`** (only on cells within ~30 km of Singapore land): tomorrow as PSI from the same CAMS
  24 h means as `tomorrow`, using NEA's official PM2.5 sub-index breakpoints. The breakpoints
  must be taken from an NEA/government source and cited in a comment with the URL. If they can't
  be verified, omit `sg` and report it. Method (NEA, same as the app's Psi.kt): round the
  concentration to an integer; segments 0–12, 13–55, 56–150, 151–250, 251–350, 351–500 map to
  PSI 0–50, 51–100, 101–200, 201–300, 301–400, 401–500, linear within a segment, rounded half
  up; above 500 the PSI is 500 (40 ug/m3 -> 83).
- **`drivers`** (every cell):
  - `rain_tonight_mm`: GFS total precipitation, 18:00 today to 06:00 tomorrow MYT, at the cell,
    1 decimal.
  - `wind850_today` / `wind850_tomorrow`: GFS 850 hPa wind, vector mean over the MYT day,
    direction FROM (deg, integer), speed km/h integer.
  - `path_fire_count_72h`: FIRMS detections (same 72 h set as `fires_72h`) within 25 km of any
    point of `path48h`. `path_fire_province`: the province holding most of them, or null if
    the count is 0. `path_fire_province` may be null when detections are outside listed provinces
    (count > 0, province null, e.g. all detections at sea). `path_fire_trend`: compare those detections' counts on the last 24 h vs the
    24 h before: "rising" if > 1.2×, "falling" if < 0.8×, else "steady"; null if count is 0.
- **`path48h`**: back-trajectory of the air arriving at the cell centre now, at 850 hPa,
  48 h back, 17 points every 3 h (index 0 = cell centre at the generation time rounded down to
  the hour, so index 8 = 24 h and index 16 = 48 h before that), lat/lon rounded to 2 decimals.
  Method: kinematic, GFS 850 hPa u/v slices every 3 h, bilinear in space, linear in time, 1 h
  integration steps (RK2). Slices: f000 and f003 of the 9 most recent completed cycles
  (`gfs_traj_cycles_utc`), plus forecast steps (f006, f009, f012) of the newest cycle up to the
  first at or after the start. A cycle is published about 5 h late, so the newest cycle lies 5
  to 10 h before "now": the newest hours use forecast steps, not analyses. Nine cycles are
  needed because the path reaches back to 43 h before the newest cycle at worst and 8 cycles
  (f000/f003) only reach 42 h. If the newest cycle's files are not all on the bucket, the cycle
  before it is used for the whole wind set. Stop early (shorter list) if the path leaves
  90–125 E, 12 S–14 N. GFS GRIB data per cycle is cached like CAMS so each cycle is downloaded
  once.
- **`fires_daily`**: same provinces as `fires_72h` (top 5), `counts` = [48–72 h ago,
  24–48 h ago, last 24 h].
- **`fire_bins`**: all 72 h detections in the FIRMS bbox binned to 0.25° (bin centre lat,
  lon, count), at most 1500 bins (largest counts kept).
- File under 500 KB. Breaking changes go to `/v3/`.

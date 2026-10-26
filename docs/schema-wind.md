# wind.json (v2/wind.json)

850 hPa wind for the app's time slider, and five days of fire detections. Published next to the
v2 file at `https://greg-celestara.github.io/hazemy-data/v2/wind.json`. Same hourly job as
schema.md and schema-v2.md. Every value is a model estimate or a satellite detection, not a
station reading. Body under 800,000 bytes (about 215 KB gzipped in practice).

**The file can be absent.** It is built in its own guarded step after v2; if anything in it
fails the job logs `ERROR: wind.json failed, publishing without it.` and publishes no file. v1
and v2 are never affected. The app must treat a missing, unreadable or invalid file as "show
the v1.5.3 screen".

**Staleness.** If `generated_utc` is more than 6 h before the app's clock, treat the file as
missing. The file is not restored from an earlier run.

## Format

Compact JSON (no spaces).

| Field | Meaning |
|---|---|
| `schema` | `"hazemy-wind/1"` |
| `generated_utc` | When the job ran. |
| `start_utc` | "Now" for the slider: `generated_utc` rounded down to the hour. Back-paths that arrive "now" arrive here. |
| `gfs_cycle_utc` | Newest GFS cycle used (the forecast slices come from it). |
| `level_hpa` | `850` |
| `grid` | `{"lat_first": -12.0, "lon_first": 90.0, "step_deg": 0.5, "nlat": 53, "nlon": 71}` |
| `scale_ms` | `0.5`: metres per second per integer step. |
| `step_h` | `3`: hours between `times` entries. |
| `times` | Ascending, contiguous, every 3 h. 49 or 50 entries covering `[start_utc - 96 h, start_utc + 48 h]`: the first is at or before `start_utc - 96 h`, the last at or after `start_utc + 48 h`. |
| `times[].valid_utc` | The instant the slice is valid for. |
| `times[].cycle_utc`, `fhr` | The GFS cycle and forecast hour it came from (analysis f000/f003 for the past, the newest cycle's forecast after it). |
| `times[].u`, `v` | Base64 (standard alphabet, padded) of 3,763 signed bytes, see below. |
| `fires_updated_utc` | Fetch time of the FIRMS detections used, or `null` when there were none. |
| `provinces` | `[[name, country], ...]` sorted by name then country; only provinces used by a fire bin. Country is `ID`, `MY` or `BN`. |
| `fires_days` | Five entries, newest first (see below). |

## Decoding the wind

The grid has 53 rows and 71 columns, 0.5 degree: rows run **south to north** (row 0 is 12.0 S,
row 52 is 14.0 N), columns west to east (column 0 is 90.0 E, column 70 is 125.0 E), row-major:
`index = row * 71 + col`. Each `u` (eastward) and `v` (northward) is the base64 of those 3,763
bytes, each a signed 8-bit integer. The wind in m/s is `int8 * scale_ms`. The byte -128 is never
written; values beyond +/-63.5 m/s are clipped to +/-127.

Worked example: the latitude of row 24 is `-12.0 + 24 * 0.5 = 0.0` and the longitude of column
60 is `90.0 + 60 * 0.5 = 120.0`, so the wind at 0 N, 120 E is at index `24 * 71 + 60 = 1764`.
If decoded byte 1764 of `u` is `20` and of `v` is `-6`, the wind there is `u = 10.0 m/s`
(blowing towards the east) and `v = -3.0 m/s` (towards the south).

Between slices the wind is linear in time; in space it is bilinear. The path is a 1 h step RK2
(Heun) back-trajectory, as in `hazemy/trajectory.py`; `hazemy.windfile.decode` returns the
arrays in the form that function takes and is the reference the app's port is tested against
(`scripts/wind_path_fixture.py` writes hourly reference paths from a real file).

## Fire days

`fires_days[k]` (k = 0 newest) has `end_utc = start_utc - 24 h * k` and
`start_utc = end_utc - 24 h`, and holds the detections acquired in `(start_utc, end_utc]`. Day 0 also holds detections acquired
after its `end_utc` (between the rounded-down hour and generation), so the newest overpass is
never dropped.
Entry 0 ends at the file's `start_utc`; the five windows together cover the 120 h before it.
Low-confidence detections are dropped.

`bins` is `[[lat, lon, count, p], ...]`: detections counted on a 0.25 degree grid, `lat` and
`lon` the bin centre, largest first (ties by latitude, then longitude), at most 1,500 per day.
`p` is the index into `provinces` of the most common province among the bin's detections (ties
by name), or `-1` when none of them is inside a province (sea, other countries).

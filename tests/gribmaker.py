"""Builds tiny GRIB2 messages with eccodes so tests run offline on known values."""
from __future__ import annotations

import numpy as np
from eccodes import (codes_get_message, codes_grib_new_from_samples, codes_release, codes_set,
                     codes_set_values)

PM2P5_PARAM_ID = 210073
UGRD_ID, VGRD_ID = 131, 132  # eccodes paramIds for U/V wind components


def make_message(values, *, lat_first, lon_first, di, dj, param_id, date=20261019, hhmm=0,
                 step=0, level_type=None, level=None) -> bytes:
    """One regular lat/lon message; rows run north to south like the real CAMS and GFS files."""
    nj, ni = values.shape
    handle = codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, val in (("Ni", ni), ("Nj", nj), ("latitudeOfFirstGridPointInDegrees", lat_first),
                         ("longitudeOfFirstGridPointInDegrees", lon_first),
                         ("latitudeOfLastGridPointInDegrees", lat_first - dj * (nj - 1)),
                         ("longitudeOfLastGridPointInDegrees", lon_first + di * (ni - 1)),
                         ("iDirectionIncrementInDegrees", di),
                         ("jDirectionIncrementInDegrees", dj), ("jScansPositively", 0),
                         ("paramId", param_id), ("dataDate", date), ("dataTime", hhmm),
                         ("step", step)):
            codes_set(handle, key, val)
        if level_type:
            codes_set(handle, "typeOfFirstFixedSurface", level_type)
            codes_set(handle, "level", level)
        codes_set_values(handle, np.asarray(values, dtype=float).ravel())
        return codes_get_message(handle)
    finally:
        codes_release(handle)

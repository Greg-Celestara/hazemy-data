"""DOE Malaysia PM2.5 -> API conversion, API bands and the 8-point compass."""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

# (conc_lo, conc_hi, api_lo, api_hi) per DOE API_Calculation.pdf, concentrations in ug/m3.
BREAKPOINTS = (
    (0.0, 12.0, 0, 50),
    (12.1, 50.4, 51, 100),
    (50.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
)
MAX_API = 500
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def round_half_up(value: float, places: int = 0) -> Decimal:
    """Round half up (not banker's rounding) to `places` decimals."""
    quantum = Decimal(1).scaleb(-places)
    return Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP)


def pm25_to_api(pm25: float) -> int:
    """DOE PM2.5 sub-index of a 24 h mean. Concentration is rounded to 1 decimal first."""
    if math.isnan(pm25):
        raise ValueError("PM2.5 is NaN; refusing to convert.")
    conc = float(round_half_up(max(pm25, 0.0), 1))
    # Thresholds follow DOE's own Excel formula: each row owns up to just below the next start.
    for index, (c_lo, c_hi, i_lo, i_hi) in enumerate(BREAKPOINTS):
        next_lo = BREAKPOINTS[index + 1][0] if index + 1 < len(BREAKPOINTS) else None
        if next_lo is None or conc < next_lo:
            if conc > c_hi:
                return MAX_API
            api = (i_hi - i_lo) / (c_hi - c_lo) * (conc - c_lo) + i_lo
            return int(round_half_up(api + 1e-9, 0))
    return MAX_API


def band(api: int) -> int:
    """1 Baik, 2 Sederhana, 3 Tidak Sihat, 4 Sangat Tidak Sihat, 5 Berbahaya."""
    for number, upper in enumerate((50, 100, 200, 300), start=1):
        if api <= upper:
            return number
    return 5


def compass(from_deg: float) -> str:
    return COMPASS[int(((from_deg % 360) + 22.5) // 45) % 8]

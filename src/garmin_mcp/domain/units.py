"""Unit conversions between the FIT wire format and something queryable.

FIT stores values in whatever unit was cheapest to encode on a watch in 2008:
angles as 32-bit semicircles, speeds in mm/s, distances in centimetres. We
normalise everything to SI at ingestion time so that no query — and no LLM
reading the output — ever has to know that.
"""

from __future__ import annotations

# A semicircle is 2^31 units per 180 degrees. This exact constant is specified
# by the FIT protocol; do not replace it with an approximation.
SEMICIRCLES_TO_DEGREES = 180.0 / (2**31)

# Sports where the watch reports one crank/pedal revolution per "cadence" unit
# and where doubling would be wrong.
_CYCLING_SPORTS = frozenset({"cycling", "e_biking", "hand_cycling"})

# Sports where FIT reports strides per minute for ONE leg, so the human-facing
# number (steps per minute) is twice the stored value.
_RUNNING_SPORTS = frozenset({"running", "walking", "hiking"})


def semicircles_to_degrees(value: int | None) -> float | None:
    """Convert a FIT position field to decimal degrees.

    Returns None for missing values and for the 0x7FFFFFFF sentinel that some
    devices write when the GPS has no fix.
    """
    if value is None or value == 0x7FFFFFFF:
        return None
    return value * SEMICIRCLES_TO_DEGREES


def normalize_cadence(sport: str | None, cadence: float | None) -> float | None:
    """Return cadence in the unit a human expects for that sport.

    Running: FIT counts one leg, so 85 stored means 170 steps per minute.
    Cycling: already revolutions per minute, left alone.
    Unknown sports are left alone rather than silently doubled.
    """
    if cadence is None:
        return None
    if sport in _RUNNING_SPORTS:
        return cadence * 2
    return cadence


def mps_to_pace_s_per_km(speed_mps: float | None) -> float | None:
    """Convert speed to running pace in seconds per kilometre.

    Returns None at or below zero: a stopped athlete has infinite pace, and
    emitting `inf` into JSON breaks strict parsers downstream.
    """
    if speed_mps is None or speed_mps <= 0:
        return None
    return 1000.0 / speed_mps


def mps_to_kmh(speed_mps: float | None) -> float | None:
    """Convert speed to km/h, the unit cyclists actually use."""
    if speed_mps is None:
        return None
    return speed_mps * 3.6


def format_pace(pace_s_per_km: float | None) -> str | None:
    """Render a pace as `M:SS/km` for human-facing output."""
    if pace_s_per_km is None or pace_s_per_km <= 0:
        return None
    total = round(pace_s_per_km)
    return f"{total // 60}:{total % 60:02d}/km"


def format_duration(seconds: float | None) -> str | None:
    """Render a duration as `H:MM:SS`, dropping the hour when it is zero."""
    if seconds is None or seconds < 0:
        return None
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def is_cycling(sport: str | None) -> bool:
    return sport in _CYCLING_SPORTS


def is_running(sport: str | None) -> bool:
    return sport in _RUNNING_SPORTS

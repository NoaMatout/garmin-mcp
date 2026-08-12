"""Unit conversions between the FIT wire format and something queryable.

FIT stores values in whatever unit was cheapest to encode on a watch in 2008:
angles as 32-bit semicircles, speeds in mm/s, distances in centimetres. We
normalise everything to SI at ingestion time so that no query — and no LLM
reading the output — ever has to know that.
"""

from __future__ import annotations

from enum import StrEnum

# A semicircle is 2^31 units per 180 degrees. This exact constant is specified
# by the FIT protocol; do not replace it with an approximation.
SEMICIRCLES_TO_DEGREES = 180.0 / (2**31)


class CadenceUnit(StrEnum):
    """What a cadence number actually counts.

    FIT stores "cadence" in three incompatible units depending on the sport,
    and the field is the same number in all three cases. Getting this wrong
    silently halves or doubles a metric that athletes read closely.
    """

    STEPS_PER_MIN = "spm"  # running: FIT counts one leg, humans count both
    RPM = "rpm"  # cycling: crank revolutions, already correct
    STROKES_PER_MIN = "spm_swim"  # swimming/rowing: strokes, already correct
    UNKNOWN = "unknown"


# Sports where FIT reports strides for ONE leg.
_STRIDE_SPORTS = frozenset({"running", "walking", "hiking", "trail_running", "snowshoeing"})

# Sports reporting crank/pedal revolutions per minute.
_RPM_SPORTS = frozenset(
    {
        "cycling",
        "e_biking",
        "hand_cycling",
        "cyclocross",
        "mountain_biking",
        "gravel_cycling",
        "indoor_cycling",
        "virtual_activity",
    }
)

# Sports reporting strokes per minute.
_STROKE_SPORTS = frozenset(
    {
        "swimming",
        "rowing",
        "paddling",
        "kayaking",
        "stand_up_paddleboarding",
        "open_water_swimming",
        "surfing",
        "windsurfing",
    }
)

# Sub-sports unambiguous enough to classify a sport FIT reports as `generic`.
# Values shared between families (`road` is used for both running and cycling)
# are deliberately absent.
_STRIDE_SUB_SPORTS = frozenset({"treadmill", "trail", "track", "street", "indoor_running"})
_RPM_SUB_SPORTS = frozenset(
    {"indoor_cycling", "spin", "mountain", "cyclocross", "virtual_activity"}
)
_STROKE_SUB_SPORTS = frozenset({"lap_swimming", "open_water", "indoor_rowing"})


def cadence_unit(
    sport: str | None,
    sub_sport: str | None = None,
    field_name: str | None = None,
) -> CadenceUnit:
    """Work out what unit a cadence value is in, for any device.

    Signals are tried strongest first:

    1. **The resolved field name.** When a file declares the sport properly,
       the FIT profile renames field 18 to `avg_running_cadence` (strides) or
       leaves it `avg_cadence` (rpm). That naming comes from the file itself,
       not from any assumption about the device, which makes it the most
       reliable signal available.
    2. **The sport.** Covers devices that write a plain `cadence` field.
    3. **The sub-sport**, but only when the sport is missing or `generic` —
       some devices report `sport=generic, sub_sport=treadmill`.

    Returns UNKNOWN rather than guessing when nothing matches: leaving a value
    unconverted is recoverable, silently doubling it is not.
    """
    if field_name:
        lowered = field_name.lower()
        if "running_cadence" in lowered or "step" in lowered:
            return CadenceUnit.STEPS_PER_MIN
        if "stroke" in lowered:
            return CadenceUnit.STROKES_PER_MIN

    if sport in _STRIDE_SPORTS:
        return CadenceUnit.STEPS_PER_MIN
    if sport in _RPM_SPORTS:
        return CadenceUnit.RPM
    if sport in _STROKE_SPORTS:
        return CadenceUnit.STROKES_PER_MIN

    if sport in (None, "generic", "training", "fitness_equipment", "multisport", "all"):
        if sub_sport in _STRIDE_SUB_SPORTS:
            return CadenceUnit.STEPS_PER_MIN
        if sub_sport in _RPM_SUB_SPORTS:
            return CadenceUnit.RPM
        if sub_sport in _STROKE_SUB_SPORTS:
            return CadenceUnit.STROKES_PER_MIN

    return CadenceUnit.UNKNOWN


def semicircles_to_degrees(value: int | None) -> float | None:
    """Convert a FIT position field to decimal degrees.

    Returns None for missing values and for the 0x7FFFFFFF sentinel that some
    devices write when the GPS has no fix.
    """
    if value is None or value == 0x7FFFFFFF:
        return None
    return value * SEMICIRCLES_TO_DEGREES


def normalize_cadence(
    sport: str | None,
    cadence: float | None,
    sub_sport: str | None = None,
    field_name: str | None = None,
) -> float | None:
    """Return cadence in the unit a human expects.

    Only stride-based values are transformed — 85 strides for one leg becomes
    170 steps per minute. Revolutions and strokes are already what the athlete
    reads, and an unclassifiable sport is left untouched.
    """
    if cadence is None:
        return None
    if cadence_unit(sport, sub_sport, field_name) is CadenceUnit.STEPS_PER_MIN:
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
    return sport in _RPM_SPORTS


def is_running(sport: str | None) -> bool:
    return sport in _STRIDE_SPORTS


def offset_from_longitude(longitude: float | None) -> int | None:
    """Rough UTC offset guessed from a GPS position.

    A last resort for files with no `activity` message, where the alternative
    is pretending the athlete lives in UTC. Fifteen degrees of longitude is one
    hour, so this lands within an hour of the truth almost everywhere — enough
    to put a session on the right calendar day, which is what weekly summaries
    depend on. Political timezones and DST are not modelled, and callers are
    expected to record that the value was inferred.
    """
    if longitude is None or not -180.0 <= longitude <= 180.0:
        return None
    return round(longitude / 15.0) * 3600

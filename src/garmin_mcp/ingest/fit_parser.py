"""FIT → domain models.

This is the heart of the project, and the part with the most folklore in it.
Things worth knowing before editing:

* **A FIT file is a message stream, not "an activity."** A normal run holds one
  `session` message. A triathlon recorded with the Multisport profile holds
  several — swim, T1, bike, T2, run — with transitions being real sessions of
  `sport = transition`, plus one `activity` message declaring `num_sessions`.
  All the `record` messages for the whole event sit in that single file, so
  they must be assigned back to their session by time window.

* **`enhanced_*` fields supersede their plain counterparts.** Garmin added them
  when 16-bit fields ran out of range (altitude below sea level, speeds above
  65 km/h). When both exist the enhanced one wins.

* **fitdecode applies the FIT profile scaling for us**, so speeds come out in
  m/s and distances in metres. Positions are the exception: they stay in
  semicircles and we convert them ourselves.

* **Unknown fields are kept, not dropped.** Anything we do not map explicitly
  lands in `extra` at session level, so a future device never silently loses
  data.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import fitdecode

from garmin_mcp.domain.models import (
    Activity,
    Lap,
    ParsedFit,
    PlannedStep,
    Record,
    Source,
    synthetic_activity_id,
)
from garmin_mcp.domain.units import (
    normalize_cadence,
    offset_from_longitude,
    semicircles_to_degrees,
)
from garmin_mcp.errors import FitParseError
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

# Session fields mapped to explicit columns; everything else goes to `extra`.
_MAPPED_SESSION_FIELDS = frozenset(
    {
        "start_time",
        "timestamp",
        "sport",
        "sub_sport",
        "event",
        "event_type",
        "total_elapsed_time",
        "total_timer_time",
        "total_distance",
        "total_ascent",
        "total_descent",
        "total_calories",
        "avg_speed",
        "max_speed",
        "enhanced_avg_speed",
        "enhanced_max_speed",
        "avg_heart_rate",
        "max_heart_rate",
        "avg_cadence",
        "max_cadence",
        "avg_running_cadence",
        "max_running_cadence",
        "avg_power",
        "max_power",
        "normalized_power",
        "intensity_factor",
        "training_stress_score",
        "total_training_effect",
        "total_anaerobic_training_effect",
        "avg_temperature",
        "pool_length",
        "total_strokes",
        "num_laps",
        "start_position_lat",
        "start_position_long",
        "message_index",
    }
)


# ─── low-level field access ───────────────────────────────────────────


def _val(msg: fitdecode.FitDataMessage, name: str, default: Any = None) -> Any:
    """Read a field, returning `default` when absent or invalid.

    FIT files from real devices routinely omit fields mid-recording (GPS drops,
    strap disconnects), so absence is normal and must never raise.
    """
    try:
        if not msg.has_field(name):
            return default
        value = msg.get_value(name)
    except (KeyError, IndexError, ValueError, TypeError):
        return default
    return default if value is None else value


def _first(msg: fitdecode.FitDataMessage, *names: str, default: Any = None) -> Any:
    """Return the first field that is present — used for enhanced/plain pairs."""
    for name in names:
        value = _val(msg, name)
        if value is not None:
            return value
    return default


def _first_named(msg: fitdecode.FitDataMessage, *names: str) -> tuple[str | None, Any]:
    """Like `_first`, but also reports which field matched.

    Which name won is itself information: a file that resolves field 18 to
    `avg_running_cadence` is telling us the value counts strides, not crank
    revolutions.
    """
    for name in names:
        value = _val(msg, name)
        if value is not None:
            return name, value
    return None, None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    """Normalise a FIT enum to a string.

    fitdecode resolves known enums to their profile name, but an unrecognised
    value comes through as a raw int — which we keep rather than discard.
    """
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _as_heart_rate(value: Any) -> int | None:
    """Read a heart rate, treating zero as missing.

    A living athlete never has a heart rate of zero, but some writers — the
    Zwift file in the test corpus among them — use 0 instead of the FIT invalid
    sentinel when no strap is connected. Left alone it drags every average down.

    Deliberately not applied to cadence or power, where zero is a real reading:
    a cyclist coasting downhill produces exactly that.
    """
    rate = _as_int(value)
    return None if rate is None or rate <= 0 else rate


def _as_utc(value: Any) -> datetime | None:
    """Coerce a FIT date_time to an aware UTC datetime."""
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ─── file hashing ─────────────────────────────────────────────────────


def hash_fit_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_fit_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ─── message extraction ───────────────────────────────────────────────


class _RawFit:
    """Messages collected in one pass over the file."""

    def __init__(self) -> None:
        self.file_id: fitdecode.FitDataMessage | None = None
        # Every `activity` message, not just the first: a file carrying several
        # is a concatenation of independent recordings, which is the signal
        # that tells a chained file apart from a genuine triathlon.
        self.activities: list[fitdecode.FitDataMessage] = []
        self.sessions: list[fitdecode.FitDataMessage] = []
        self.laps: list[fitdecode.FitDataMessage] = []
        self.records: list[fitdecode.FitDataMessage] = []
        # The structured workout the session was run from, when there was one.
        self.workout: fitdecode.FitDataMessage | None = None
        self.workout_steps: list[fitdecode.FitDataMessage] = []
        self.truncated = False
        self.truncation_reason: str | None = None

    @property
    def activity(self) -> fitdecode.FitDataMessage | None:
        return self.activities[0] if self.activities else None


def _read_messages(path: Path) -> _RawFit:
    """Read every message, keeping whatever survives a mid-file failure.

    Files truncated by an interrupted download, and files some writers emit
    with an undefined local message type partway through, still hold complete
    sessions before the break. Discarding an entire ride because its last
    kilobyte is damaged loses real data, so the read stops at the error and
    keeps what came before — as long as a session made it through.
    """
    raw = _RawFit()
    try:
        with fitdecode.FitReader(str(path)) as reader:
            for frame in reader:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                match frame.name:
                    case "record":
                        raw.records.append(frame)
                    case "lap":
                        raw.laps.append(frame)
                    case "session":
                        raw.sessions.append(frame)
                    case "file_id":
                        raw.file_id = raw.file_id or frame
                    case "activity":
                        raw.activities.append(frame)
                    case "workout":
                        raw.workout = raw.workout or frame
                    case "workout_step":
                        raw.workout_steps.append(frame)
    except FileNotFoundError:
        raise
    except Exception as exc:  # fitdecode raises a family of parse errors
        raw.truncated = True
        raw.truncation_reason = f"{type(exc).__name__}: {exc}"
        if not raw.sessions:
            # Nothing usable came out before the break.
            raise FitParseError(f"could not decode FIT file: {exc}", path=str(path)) from exc
        log.warning(
            "fit.partial_recovery",
            path=path.name,
            reason=raw.truncation_reason,
            sessions=len(raw.sessions),
            records=len(raw.records),
        )

    if not raw.sessions:
        raise FitParseError(
            "FIT file contains no session message — not an activity file "
            "(settings, courses and monitoring files are not supported)",
            path=str(path),
        )
    return raw


# ─── time-window assignment ───────────────────────────────────────────


def _resolve_session_start(
    msg: fitdecode.FitDataMessage,
    raw: _RawFit,
) -> tuple[datetime | None, str]:
    """Find a session's start instant, falling back when the field is unusable.

    Old firmware sometimes writes `start_time` as a raw integer that the FIT
    profile never resolves to a date — the fr70 ANT-FS dumps in the test corpus
    do exactly that, while the very same file carries a perfectly good
    `local_timestamp` in its activity message. Rather than drop the ride, try
    progressively weaker anchors and report which one was used, so the
    reconstruction is visible in the data instead of silent.
    """
    elapsed = _as_float(_first(msg, "total_elapsed_time", "total_timer_time")) or 0.0

    if (start := _as_utc(_val(msg, "start_time"))) is not None:
        return start, "start_time"

    # The session's own end stamp, walked back by its duration.
    if (end := _as_utc(_val(msg, "timestamp"))) is not None:
        return end - timedelta(seconds=elapsed), "session_timestamp"

    # The first sample that carries a real date.
    for record in raw.records:
        if (ts := _as_utc(_val(record, "timestamp"))) is not None:
            return ts, "first_record"

    # The activity message. `local_timestamp` is wall-clock rather than UTC, so
    # this places the activity on the right day but not the right instant.
    if raw.activity is not None:
        if (ts := _as_utc(_val(raw.activity, "timestamp"))) is not None:
            return ts - timedelta(seconds=elapsed), "activity_timestamp"
        if (ts := _as_utc(_val(raw.activity, "local_timestamp"))) is not None:
            return ts - timedelta(seconds=elapsed), "activity_local_timestamp"

    if raw.file_id is not None and (ts := _as_utc(_val(raw.file_id, "time_created"))):
        return ts, "file_created"

    return None, "none"


def _session_window(
    msg: fitdecode.FitDataMessage, raw: _RawFit
) -> tuple[datetime, datetime] | None:
    """[start, end] of a session, from its start time and elapsed duration."""
    start, _ = _resolve_session_start(msg, raw)
    if start is None:
        return None
    elapsed = _as_float(_first(msg, "total_elapsed_time", "total_timer_time")) or 0.0
    return start, start + timedelta(seconds=elapsed)


def _assign_by_window(
    items: Sequence[tuple[datetime, Any]],
    windows: Sequence[tuple[datetime, datetime]],
) -> list[list[Any]]:
    """Bucket timestamped items into ordered, non-overlapping time windows.

    Items falling in a gap between windows — a paused watch, or the seconds
    between one leg ending and the next starting — attach to the preceding
    window rather than being dropped: losing samples is worse than a slightly
    generous boundary.

    Items *before the first window* are discarded, however. Devices record
    while acquiring GPS: a Fenix 2 file in the test corpus starts logging 45
    minutes before the athlete pressed start. Attaching those to the first
    session would give them a negative `elapsed_s` and corrupt every stream
    query, and they are not part of the activity in the first place.
    """
    buckets: list[list[Any]] = [[] for _ in windows]
    if not windows:
        return buckets
    starts = [w[0] for w in windows]

    dropped = 0
    for ts, item in items:
        idx = bisect_right(starts, ts) - 1
        if idx < 0:
            dropped += 1
            continue
        buckets[idx].append(item)

    if dropped:
        log.debug("fit.pre_activity_samples_dropped", count=dropped)
    return buckets


# ─── builders ─────────────────────────────────────────────────────────


def _build_record(msg: fitdecode.FitDataMessage, session_start: datetime) -> Record | None:
    ts = _as_utc(_val(msg, "timestamp"))
    if ts is None:
        return None

    return Record(
        ts=ts,
        elapsed_s=(ts - session_start).total_seconds(),
        lat=semicircles_to_degrees(_as_int(_val(msg, "position_lat"))),
        lon=semicircles_to_degrees(_as_int(_val(msg, "position_long"))),
        altitude_m=_as_float(_first(msg, "enhanced_altitude", "altitude")),
        distance_m=_as_float(_val(msg, "distance")),
        speed_mps=_as_float(_first(msg, "enhanced_speed", "speed")),
        heart_rate=_as_heart_rate(_val(msg, "heart_rate")),
        cadence=_as_int(_val(msg, "cadence")),
        power_w=_as_int(_val(msg, "power")),
        temperature_c=_as_int(_val(msg, "temperature")),
        grade=_as_float(_val(msg, "grade")),
        vertical_oscillation_mm=_as_float(_val(msg, "vertical_oscillation")),
        vertical_ratio=_as_float(_val(msg, "vertical_ratio")),
        stance_time_ms=_as_float(_val(msg, "stance_time")),
        stance_time_percent=_as_float(_val(msg, "stance_time_percent")),
        step_length_mm=_as_float(_val(msg, "step_length")),
        left_right_balance=_as_float(_val(msg, "left_right_balance")),
        respiration_rate=_as_float(_first(msg, "enhanced_respiration_rate", "respiration_rate")),
        accumulated_power_w=_as_int(_val(msg, "accumulated_power")),
    )


def _build_lap(
    msg: fitdecode.FitDataMessage,
    lap_index: int,
    sport: str | None,
    sub_sport: str | None = None,
) -> Lap:
    return Lap(
        lap_index=lap_index,
        start_time_utc=_as_utc(_val(msg, "start_time")),
        total_timer_time_s=_as_float(_val(msg, "total_timer_time")),
        total_distance_m=_as_float(_val(msg, "total_distance")),
        avg_speed_mps=_as_float(_first(msg, "enhanced_avg_speed", "avg_speed")),
        max_speed_mps=_as_float(_first(msg, "enhanced_max_speed", "max_speed")),
        avg_heart_rate=_as_heart_rate(_val(msg, "avg_heart_rate")),
        max_heart_rate=_as_heart_rate(_val(msg, "max_heart_rate")),
        avg_cadence=_cadence(msg, sport, sub_sport, "avg_running_cadence", "avg_cadence"),
        avg_power_w=_as_float(_val(msg, "avg_power")),
        normalized_power_w=_as_float(_val(msg, "normalized_power")),
        total_ascent_m=_as_float(_val(msg, "total_ascent")),
        total_descent_m=_as_float(_val(msg, "total_descent")),
        total_calories=_as_int(_val(msg, "total_calories")),
        intensity=_as_str(_val(msg, "intensity")),
        lap_trigger=_as_str(_val(msg, "lap_trigger")),
        wkt_step_index=_as_int(_val(msg, "wkt_step_index")),
    )


def _build_planned_steps(raw: _RawFit) -> list[PlannedStep]:
    """Read the prescription the watch was following.

    Targets are stored the same way workouts are written: a speed range in
    metres per second for a pace zone, zeroes when the step is `open`, which
    is what an athlete who prefers no alerts ends up with.
    """
    steps: list[PlannedStep] = []
    name = _as_str(_val(raw.workout, "wkt_name")) if raw.workout is not None else None

    for msg in raw.workout_steps:
        index = _as_int(_val(msg, "message_index"))
        if index is None:
            continue
        duration_type = _as_str(_val(msg, "duration_type"))
        duration = _as_float(_first(msg, "duration_time", "duration_distance", "duration_value"))
        target_type = _as_str(_val(msg, "target_type"))
        low = _as_float(_val(msg, "custom_target_value_low"))
        high = _as_float(_val(msg, "custom_target_value_high"))

        steps.append(
            PlannedStep(
                step_index=index,
                intensity=_as_str(_val(msg, "intensity")),
                duration_type=duration_type,
                duration_value=duration,
                target_type=None if target_type == "open" else target_type,
                target_low=low or None,
                target_high=high or None,
                repeat_from_step=_as_int(_val(msg, "duration_step")),
                repeat_count=_as_int(_val(msg, "repeat_steps")),
                name=name,
            )
        )
    return steps


def _session_extra(msg: fitdecode.FitDataMessage) -> dict[str, Any]:
    """Everything the schema does not have a column for."""
    extra: dict[str, Any] = {}
    for fld in msg.fields:
        if fld.name in _MAPPED_SESSION_FIELDS or fld.value is None:
            continue
        value = fld.value
        # JSON cannot hold datetimes or byte strings; stringify rather than drop.
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, bytes | bytearray):
            value = value.hex()
        elif isinstance(value, tuple | list):
            value = [str(v) for v in value]
        elif not isinstance(value, str | int | float | bool):
            value = str(value)
        extra[fld.name] = value
    return extra


def _build_activity(
    msg: fitdecode.FitDataMessage,
    *,
    activity_id: int,
    session_index: int,
    file_hash: str,
    source: Source,
    start: datetime,
    start_source: str,
    tz_offset_seconds: int | None,
    tz_source: str,
    garmin_activity_id: int | None,
    device_product: str | None,
    device_serial: int | None,
    name: str | None,
) -> Activity:
    sport = _as_str(_val(msg, "sport"))
    sub_sport = _as_str(_val(msg, "sub_sport"))
    offset = tz_offset_seconds or 0

    extra = _session_extra(msg)
    # Record how the timestamps were obtained whenever they were not read
    # straight from the file, so a reconstructed value is never mistaken for a
    # measured one.
    if start_source != "start_time":
        extra["_start_time_source"] = start_source
    if tz_source != "activity_message":
        extra["_tz_offset_source"] = tz_source

    return Activity(
        activity_id=activity_id,
        source=source,
        file_hash=file_hash,
        start_time_utc=start,
        # Naive local wall-clock time: what the athlete would read on a watch.
        start_time_local=(start + timedelta(seconds=offset)).replace(tzinfo=None),
        session_index=session_index,
        garmin_activity_id=garmin_activity_id,
        sport=sport,
        sub_sport=sub_sport,
        name=name,
        tz_offset_seconds=tz_offset_seconds,
        total_timer_time_s=_as_float(_val(msg, "total_timer_time")),
        total_elapsed_time_s=_as_float(_val(msg, "total_elapsed_time")),
        total_distance_m=_as_float(_val(msg, "total_distance")),
        total_ascent_m=_as_float(_val(msg, "total_ascent")),
        total_descent_m=_as_float(_val(msg, "total_descent")),
        total_calories=_as_int(_val(msg, "total_calories")),
        avg_speed_mps=_as_float(_first(msg, "enhanced_avg_speed", "avg_speed")),
        max_speed_mps=_as_float(_first(msg, "enhanced_max_speed", "max_speed")),
        avg_heart_rate=_as_heart_rate(_val(msg, "avg_heart_rate")),
        max_heart_rate=_as_heart_rate(_val(msg, "max_heart_rate")),
        avg_cadence=_cadence(msg, sport, sub_sport, "avg_running_cadence", "avg_cadence"),
        max_cadence=_cadence(msg, sport, sub_sport, "max_running_cadence", "max_cadence"),
        avg_power_w=_as_float(_val(msg, "avg_power")),
        max_power_w=_as_float(_val(msg, "max_power")),
        normalized_power_w=_as_float(_val(msg, "normalized_power")),
        intensity_factor=_as_float(_val(msg, "intensity_factor")),
        training_stress_score=_as_float(_val(msg, "training_stress_score")),
        aerobic_training_effect=_as_float(_val(msg, "total_training_effect")),
        anaerobic_training_effect=_as_float(_val(msg, "total_anaerobic_training_effect")),
        avg_temperature_c=_as_float(_val(msg, "avg_temperature")),
        pool_length_m=_as_float(_val(msg, "pool_length")),
        total_strokes=_as_int(_val(msg, "total_strokes")),
        num_laps=_as_int(_val(msg, "num_laps")),
        device_product=device_product,
        device_serial=device_serial,
        start_lat=semicircles_to_degrees(_as_int(_val(msg, "start_position_lat"))),
        start_lon=semicircles_to_degrees(_as_int(_val(msg, "start_position_long"))),
        extra=extra,
    )


def _cadence(
    msg: fitdecode.FitDataMessage,
    sport: str | None,
    sub_sport: str | None,
    *names: str,
) -> float | None:
    """Read a cadence field and convert it using whatever unit it is really in."""
    field_name, value = _first_named(msg, *names)
    return normalize_cadence(sport, _as_float(value), sub_sport, field_name)


def _build_multisport_parent(
    children: Sequence[Activity],
    *,
    activity_id: int,
    file_hash: str,
    source: Source,
    garmin_activity_id: int | None,
    name: str | None,
) -> Activity:
    """Aggregate a triathlon's legs into one parent row.

    The parent exists so "my triathlon on June 15th, 2h47" remains a single
    object, while the children keep per-discipline numbers meaningful. It owns
    no records or laps — those belong to the legs.
    """
    first = min(children, key=lambda a: a.start_time_utc)

    def _sum(attr: str) -> float | None:
        values = [v for c in children if (v := getattr(c, attr)) is not None]
        return sum(values) if values else None

    def _max(attr: str) -> Any:
        values = [v for c in children if (v := getattr(c, attr)) is not None]
        return max(values) if values else None

    # Heart rate is averaged over moving time, not over legs: a 3-minute
    # transition must not count as much as a 90-minute bike leg.
    weighted = [
        (c.avg_heart_rate, c.total_timer_time_s)
        for c in children
        if c.avg_heart_rate is not None and c.total_timer_time_s
    ]
    total_weight = sum(w for _, w in weighted)
    avg_hr = round(sum(hr * w for hr, w in weighted) / total_weight) if total_weight else None

    return Activity(
        activity_id=activity_id,
        source=source,
        file_hash=file_hash,
        start_time_utc=first.start_time_utc,
        start_time_local=first.start_time_local,
        session_index=-1,  # sorts before every leg
        garmin_activity_id=garmin_activity_id,
        sport="multisport",
        sub_sport=None,
        name=name,
        tz_offset_seconds=first.tz_offset_seconds,
        total_timer_time_s=_sum("total_timer_time_s"),
        total_elapsed_time_s=_sum("total_elapsed_time_s"),
        total_distance_m=_sum("total_distance_m"),
        total_ascent_m=_sum("total_ascent_m"),
        total_descent_m=_sum("total_descent_m"),
        total_calories=int(_sum("total_calories") or 0) or None,
        avg_heart_rate=avg_hr,
        max_heart_rate=_max("max_heart_rate"),
        max_speed_mps=_max("max_speed_mps"),
        max_power_w=_max("max_power_w"),
        num_laps=int(_sum("num_laps") or 0) or None,
        device_product=first.device_product,
        device_serial=first.device_serial,
        start_lat=first.start_lat,
        start_lon=first.start_lon,
        extra={"legs": [c.sport for c in sorted(children, key=lambda a: a.start_time_utc)]},
    )


# ─── device-agnostic heuristics ───────────────────────────────────────
#
# Everything below exists because real files disagree with the specification
# in ways that only show up across a corpus of different manufacturers.

# Legs of one multisport event follow each other immediately. Anything further
# apart is a separate outing that happens to share a file.
_CONTIGUITY_TOLERANCE_S = 300


def _elapsed(msg: fitdecode.FitDataMessage) -> float:
    return _as_float(_first(msg, "total_elapsed_time", "total_timer_time")) or 0.0


def _device_identity(
    file_id: fitdecode.FitDataMessage | None,
) -> tuple[str | None, int | None]:
    """Readable device label and serial, for any manufacturer.

    `garmin_product` only resolves for Garmin hardware. Wahoo and SigmaSport
    write a bare integer, and Coros writes a `product_name` string, so the
    manufacturer is folded into the label to keep an unresolved code
    identifiable rather than a naked number.
    """
    if file_id is None:
        return None, None

    serial = _as_int(_val(file_id, "serial_number"))
    manufacturer = _as_str(_val(file_id, "manufacturer"))
    _, product = _first_named(file_id, "garmin_product", "product_name", "product")

    if isinstance(product, str) and product.strip():
        return product.strip(), serial
    if product is not None:
        return (f"{manufacturer}:{product}" if manufacturer else str(product)), serial
    return manufacturer, serial


def _dedupe_sessions(
    resolved: list[tuple[fitdecode.FitDataMessage, datetime, str]],
) -> list[tuple[fitdecode.FitDataMessage, datetime, str]]:
    """Drop sessions that are byte-for-byte repeats of one already seen.

    Concatenating a FIT file with itself is a real occurrence — some transfer
    tools do it — and produces identical session messages. Without this, one
    ride would be stored twice and every weekly total would be inflated.
    """
    seen: set[tuple[datetime, str | None, float]] = set()
    unique = []
    for item in resolved:
        msg, start, _ = item
        key = (start, _as_str(_val(msg, "sport")), round(_elapsed(msg), 3))
        if key in seen:
            log.debug("fit.duplicate_session_dropped", start=start.isoformat())
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _is_multisport(
    raw: _RawFit,
    resolved: list[tuple[fitdecode.FitDataMessage, datetime, str]],
) -> bool:
    """Decide whether several sessions are one event or several activities.

    More than one session does *not* imply a triathlon. A file can hold several
    independent recordings chained together, and treating those as legs of a
    single event would invent an activity that never happened.

    The discriminators, strongest first:

    * **More than one `activity` message** means the file is a concatenation of
      separate recordings — each declares its own session count.
    * **A single `activity` message announcing as many sessions as we found**
      is the device stating outright that this was one multisport event.
    * With no `activity` message at all, fall back to shape: legs that run
      back-to-back and cover more than one sport, or that include an explicit
      `transition`.
    """
    if len(resolved) <= 1:
        return False

    if len(raw.activities) > 1:
        return False

    if raw.activities:
        declared = _as_int(_val(raw.activities[0], "num_sessions"))
        if declared is not None:
            return declared == len(resolved) and declared > 1

    sports = {_as_str(_val(msg, "sport")) for msg, _, _ in resolved}
    if "transition" in sports:
        return True

    contiguous = all(
        (nxt_start - (start + timedelta(seconds=_elapsed(msg)))).total_seconds()
        <= _CONTIGUITY_TOLERANCE_S
        for (msg, start, _), (_, nxt_start, _) in pairwise(resolved)
    )
    return contiguous and len(sports) > 1


def _resolve_tz_offset(
    raw: _RawFit,
    sessions: Sequence[fitdecode.FitDataMessage],
) -> tuple[int | None, str]:
    """Determine the athlete's UTC offset, and say where it came from.

    The `activity` message pairs a UTC and a local stamp, which is exact. Many
    older devices omit that message entirely, and the alternative — pretending
    the athlete lives in UTC — silently files a Sunday evening run under
    Monday. Guessing from GPS longitude is imprecise but lands on the right
    calendar day, which is what weekly summaries actually depend on.
    """
    if raw.activity is not None and (offset := _tz_offset(raw.activity)) is not None:
        return offset, "activity_message"

    longitude = None
    for msg in sessions:
        if (
            longitude := semicircles_to_degrees(_as_int(_val(msg, "start_position_long")))
        ) is not None:
            break
    if longitude is None:
        for record in raw.records:
            if (
                longitude := semicircles_to_degrees(_as_int(_val(record, "position_long")))
            ) is not None:
                break

    if (offset := offset_from_longitude(longitude)) is not None:
        return offset, "longitude_estimate"

    return None, "unavailable"


# ─── public entry point ───────────────────────────────────────────────


def parse_fit(
    path: Path,
    *,
    file_hash: str | None = None,
    source: Source = "manual",
    garmin_activity_id: int | None = None,
    activity_name: str | None = None,
) -> ParsedFit:
    """Parse a FIT file into activities, laps and records.

    `garmin_activity_id` is supplied when the file came from a Garmin download;
    manual imports get a deterministic synthetic id derived from the content
    hash, so re-importing the same file updates rather than duplicates.
    """
    path = Path(path)
    if not path.exists():
        raise FitParseError(f"file does not exist: {path}", path=str(path))

    file_hash = file_hash or hash_fit_file(path)
    raw = _read_messages(path)

    device_product, device_serial = _device_identity(raw.file_id)

    # Resolve each session's start, tolerating firmware that omits it, then
    # drop the duplicates a concatenated file produces.
    resolved: list[tuple[fitdecode.FitDataMessage, datetime, str]] = []
    for msg in raw.sessions:
        start, start_source = _resolve_session_start(msg, raw)
        if start is not None:
            resolved.append((msg, start, start_source))

    if not resolved:
        raise FitParseError(
            "no session carries a usable start time, and none could be "
            "reconstructed from the file's other timestamps",
            path=str(path),
        )

    resolved.sort(key=lambda item: item[1])
    resolved = _dedupe_sessions(resolved)

    sessions = [msg for msg, _, _ in resolved]
    windows = [(start, start + timedelta(seconds=_elapsed(msg))) for msg, start, _ in resolved]

    is_multisport = _is_multisport(raw, resolved)
    num_sessions = len(resolved)

    tz_offset_seconds, tz_source = _resolve_tz_offset(raw, sessions)

    planned = _build_planned_steps(raw)

    lap_buckets = _assign_by_window(
        [(ts, m) for m in raw.laps if (ts := _as_utc(_val(m, "start_time"))) is not None], windows
    )
    record_buckets = _assign_by_window(
        [(ts, m) for m in raw.records if (ts := _as_utc(_val(m, "timestamp"))) is not None],
        windows,
    )

    activities: list[Activity] = []
    for index, (session_msg, start, start_source) in enumerate(resolved):
        # In a multisport file the Garmin id belongs to the parent, so each leg
        # gets its own synthetic id.
        if is_multisport or garmin_activity_id is None:
            activity_id = synthetic_activity_id(file_hash, index)
        else:
            activity_id = garmin_activity_id

        activity = _build_activity(
            session_msg,
            activity_id=activity_id,
            session_index=index,
            file_hash=file_hash,
            source=source,
            start=start,
            start_source=start_source,
            tz_offset_seconds=tz_offset_seconds,
            tz_source=tz_source,
            garmin_activity_id=None if is_multisport else garmin_activity_id,
            device_product=device_product,
            device_serial=device_serial,
            name=None if is_multisport else activity_name,
        )
        activity.laps = [
            _build_lap(m, i, activity.sport, activity.sub_sport)
            for i, m in enumerate(lap_buckets[index])
        ]
        activity.records = _attach_records(
            record_buckets[index], activity.start_time_utc, activity.laps
        )
        if raw.truncated:
            activity.extra["_truncated"] = raw.truncation_reason
        # The prescription belongs to the whole file; attach it to the single
        # session, or to nothing when the file holds several.
        if planned and len(resolved) == 1:
            activity.planned_steps = planned
            activity.workout_name = planned[0].name
        activities.append(activity)

    if is_multisport:
        parent_id = garmin_activity_id or synthetic_activity_id(file_hash, -1)
        parent = _build_multisport_parent(
            activities,
            activity_id=parent_id,
            file_hash=file_hash,
            source=source,
            garmin_activity_id=garmin_activity_id,
            name=activity_name,
        )
        for child in activities:
            child.parent_activity_id = parent_id
        activities.insert(0, parent)

    parsed = ParsedFit(
        file_hash=file_hash,
        path=str(path),
        source=source,
        activities=activities,
        num_sessions=num_sessions,
    )
    log.info(
        "fit.parsed",
        path=path.name,
        sessions=num_sessions,
        multisport=is_multisport,
        activities=len(activities),
        records=parsed.total_records,
    )
    return parsed


def _tz_offset(activity_msg: fitdecode.FitDataMessage) -> int | None:
    """Derive the UTC offset from the activity message's paired timestamps.

    FIT stores both `timestamp` (UTC) and `local_timestamp` (the same instant
    in the athlete's local time). Their difference is the offset. Rounded to a
    whole minute because some firmware writes a one-second skew.
    """
    utc = _as_utc(_val(activity_msg, "timestamp"))
    local = _val(activity_msg, "local_timestamp")
    if utc is None or not isinstance(local, datetime):
        return None
    local_naive = local.replace(tzinfo=None)
    delta = (local_naive - utc.replace(tzinfo=None)).total_seconds()
    return int(round(delta / 60.0) * 60)


def _attach_records(
    messages: Iterable[fitdecode.FitDataMessage],
    session_start: datetime,
    laps: Sequence[Lap],
) -> list[Record]:
    """Build records and tag each with the lap it falls in."""
    records = [r for m in messages if (r := _build_record(m, session_start)) is not None]
    if not laps:
        return records

    lap_starts = [lap.start_time_utc for lap in laps if lap.start_time_utc is not None]
    if len(lap_starts) != len(laps):
        return records  # incomplete lap timing: leave lap_index unset

    for record in records:
        idx = bisect_right(lap_starts, record.ts) - 1
        record.lap_index = laps[max(idx, 0)].lap_index
    return records

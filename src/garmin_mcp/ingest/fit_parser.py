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
from pathlib import Path
from typing import Any

import fitdecode

from garmin_mcp.domain.models import (
    Activity,
    Lap,
    ParsedFit,
    Record,
    Source,
    synthetic_activity_id,
)
from garmin_mcp.domain.units import normalize_cadence, semicircles_to_degrees
from garmin_mcp.errors import FitParseError
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

# Session fields mapped to explicit columns; everything else goes to `extra`.
_MAPPED_SESSION_FIELDS = frozenset(
    {
        "start_time", "timestamp", "sport", "sub_sport", "event", "event_type",
        "total_elapsed_time", "total_timer_time", "total_distance",
        "total_ascent", "total_descent", "total_calories",
        "avg_speed", "max_speed", "enhanced_avg_speed", "enhanced_max_speed",
        "avg_heart_rate", "max_heart_rate", "avg_cadence", "max_cadence",
        "avg_running_cadence", "max_running_cadence",
        "avg_power", "max_power", "normalized_power",
        "intensity_factor", "training_stress_score",
        "total_training_effect", "total_anaerobic_training_effect",
        "avg_temperature", "pool_length", "total_strokes", "num_laps",
        "start_position_lat", "start_position_long", "message_index",
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
        self.activity: fitdecode.FitDataMessage | None = None
        self.sessions: list[fitdecode.FitDataMessage] = []
        self.laps: list[fitdecode.FitDataMessage] = []
        self.records: list[fitdecode.FitDataMessage] = []


def _read_messages(path: Path) -> _RawFit:
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
                        raw.activity = raw.activity or frame
    except FileNotFoundError:
        raise
    except Exception as exc:  # fitdecode raises a family of parse errors
        raise FitParseError(f"could not decode FIT file: {exc}", path=str(path)) from exc

    if not raw.sessions:
        raise FitParseError(
            "FIT file contains no session message — not an activity file "
            "(settings, courses and monitoring files are not supported)",
            path=str(path),
        )
    return raw


# ─── time-window assignment ───────────────────────────────────────────


def _session_window(msg: fitdecode.FitDataMessage) -> tuple[datetime, datetime] | None:
    """[start, end] of a session, from its start time and elapsed duration."""
    start = _as_utc(_val(msg, "start_time"))
    if start is None:
        return None
    elapsed = _as_float(_first(msg, "total_elapsed_time", "total_timer_time")) or 0.0
    return start, start + timedelta(seconds=elapsed)


def _assign_by_window(
    items: Sequence[tuple[datetime, Any]],
    windows: Sequence[tuple[datetime, datetime]],
) -> list[list[Any]]:
    """Bucket timestamped items into ordered, non-overlapping time windows.

    Items falling in a gap between windows (a paused watch, or the seconds
    between a leg ending and the next starting) attach to the preceding window
    rather than being dropped — losing samples is worse than a slightly generous
    boundary.
    """
    buckets: list[list[Any]] = [[] for _ in windows]
    if not windows:
        return buckets
    starts = [w[0] for w in windows]

    for ts, item in items:
        idx = bisect_right(starts, ts) - 1
        if idx < 0:
            idx = 0  # sample predates the first session (device warm-up)
        buckets[idx].append(item)
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
        heart_rate=_as_int(_val(msg, "heart_rate")),
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


def _build_lap(msg: fitdecode.FitDataMessage, lap_index: int, sport: str | None) -> Lap:
    return Lap(
        lap_index=lap_index,
        start_time_utc=_as_utc(_val(msg, "start_time")),
        total_timer_time_s=_as_float(_val(msg, "total_timer_time")),
        total_distance_m=_as_float(_val(msg, "total_distance")),
        avg_speed_mps=_as_float(_first(msg, "enhanced_avg_speed", "avg_speed")),
        max_speed_mps=_as_float(_first(msg, "enhanced_max_speed", "max_speed")),
        avg_heart_rate=_as_int(_val(msg, "avg_heart_rate")),
        max_heart_rate=_as_int(_val(msg, "max_heart_rate")),
        avg_cadence=normalize_cadence(sport, _as_float(_val(msg, "avg_cadence"))),
        avg_power_w=_as_float(_val(msg, "avg_power")),
        normalized_power_w=_as_float(_val(msg, "normalized_power")),
        total_ascent_m=_as_float(_val(msg, "total_ascent")),
        total_descent_m=_as_float(_val(msg, "total_descent")),
        total_calories=_as_int(_val(msg, "total_calories")),
        intensity=_as_str(_val(msg, "intensity")),
        lap_trigger=_as_str(_val(msg, "lap_trigger")),
    )


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
    tz_offset_seconds: int | None,
    garmin_activity_id: int | None,
    device_product: str | None,
    device_serial: int | None,
    name: str | None,
) -> Activity:
    start = _as_utc(_val(msg, "start_time"))
    if start is None:
        raise FitParseError(f"session {session_index} has no start_time")

    sport = _as_str(_val(msg, "sport"))
    offset = tz_offset_seconds or 0

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
        sub_sport=_as_str(_val(msg, "sub_sport")),
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
        avg_heart_rate=_as_int(_val(msg, "avg_heart_rate")),
        max_heart_rate=_as_int(_val(msg, "max_heart_rate")),
        avg_cadence=normalize_cadence(
            sport, _as_float(_first(msg, "avg_running_cadence", "avg_cadence"))
        ),
        max_cadence=normalize_cadence(
            sport, _as_float(_first(msg, "max_running_cadence", "max_cadence"))
        ),
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
        extra=_session_extra(msg),
    )


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

    # Device identity, shared by every session in the file.
    device_serial = device_product = None
    if raw.file_id is not None:
        device_serial = _as_int(_val(raw.file_id, "serial_number"))
        device_product = _as_str(
            _first(raw.file_id, "garmin_product", "product", "manufacturer")
        )

    # Local time offset, from the activity message's paired UTC/local stamps.
    tz_offset_seconds = None
    num_sessions = len(raw.sessions)
    if raw.activity is not None:
        tz_offset_seconds = _tz_offset(raw.activity)
        num_sessions = _as_int(_val(raw.activity, "num_sessions")) or num_sessions

    sessions = sorted(raw.sessions, key=lambda m: _as_utc(_val(m, "start_time")) or datetime.min)
    windows = [w for m in sessions if (w := _session_window(m)) is not None]
    if len(windows) != len(sessions):
        raise FitParseError("a session message is missing its start_time", path=str(path))

    is_multisport = len(sessions) > 1
    lap_buckets = _assign_by_window(
        [(ts, m) for m in raw.laps if (ts := _as_utc(_val(m, "start_time"))) is not None], windows
    )
    record_buckets = _assign_by_window(
        [(ts, m) for m in raw.records if (ts := _as_utc(_val(m, "timestamp"))) is not None],
        windows,
    )

    activities: list[Activity] = []
    for index, session_msg in enumerate(sessions):
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
            tz_offset_seconds=tz_offset_seconds,
            garmin_activity_id=None if is_multisport else garmin_activity_id,
            device_product=device_product,
            device_serial=device_serial,
            name=None if is_multisport else activity_name,
        )
        activity.laps = [
            _build_lap(m, i, activity.sport) for i, m in enumerate(lap_buckets[index])
        ]
        activity.records = _attach_records(
            record_buckets[index], activity.start_time_utc, activity.laps
        )
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

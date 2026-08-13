"""Domain models — the shape of a parsed FIT file.

These dataclasses mirror the DuckDB columns one-for-one on purpose: the writer
stays a dumb field-for-field mapping, and adding a metric means touching the
model, the DDL and nothing else.

Slots are enabled because a long ride produces ~15 000 Record instances and
the per-object dict overhead is measurable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Source = Literal["garmin", "manual"]

# Bit 62 marks synthetic ids. Real Garmin activity ids are ~2e10 in 2026 and
# grow slowly, so anything at or above 2^62 can never collide with one.
_SYNTHETIC_ID_FLAG = 1 << 62


def synthetic_activity_id(file_hash: str, session_index: int) -> int:
    """Deterministic id for an activity that has no Garmin id.

    Manually imported FIT files carry no Garmin activity id, but we still need
    a stable primary key: re-importing the same file must produce the same id
    so the upsert overwrites instead of duplicating.

    Derived from the file content hash, so it is stable across machines and
    across re-parses, and independent of filename or import order.
    """
    digest = hashlib.sha256(f"{file_hash}:{session_index}".encode()).digest()
    return _SYNTHETIC_ID_FLAG | int.from_bytes(digest[:6], "big")


def is_synthetic_id(activity_id: int) -> bool:
    return bool(activity_id & _SYNTHETIC_ID_FLAG)


@dataclass(slots=True)
class Record:
    """One sample, normally one per second."""

    ts: datetime
    elapsed_s: float
    lap_index: int | None = None

    lat: float | None = None
    lon: float | None = None
    altitude_m: float | None = None
    distance_m: float | None = None
    speed_mps: float | None = None
    heart_rate: int | None = None
    cadence: int | None = None
    power_w: int | None = None
    temperature_c: int | None = None
    grade: float | None = None

    # Running dynamics — the 955 records these with a compatible strap/pod.
    vertical_oscillation_mm: float | None = None
    vertical_ratio: float | None = None
    stance_time_ms: float | None = None
    stance_time_percent: float | None = None
    step_length_mm: float | None = None
    left_right_balance: float | None = None
    respiration_rate: float | None = None
    accumulated_power_w: int | None = None

    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlannedStep:
    """One step of the structured workout an activity was run from.

    Present only when the athlete started the session from a workout on the
    watch. It is the prescription, carried inside the FIT file itself, which
    is what makes comparing intent against execution possible without linking
    anything to a Garmin id.
    """

    step_index: int
    intensity: str | None = None
    duration_type: str | None = None  # time | distance | open | repeat_until_steps_cmplt
    duration_value: float | None = None  # seconds or metres, per duration_type
    target_type: str | None = None
    target_low: float | None = None
    target_high: float | None = None
    repeat_from_step: int | None = None
    repeat_count: int | None = None
    name: str | None = None

    @property
    def is_repeat(self) -> bool:
        return self.duration_type == "repeat_until_steps_cmplt"


@dataclass(slots=True)
class Lap:
    """One lap or interval within a session."""

    lap_index: int
    start_time_utc: datetime | None = None

    total_timer_time_s: float | None = None
    total_distance_m: float | None = None
    avg_speed_mps: float | None = None
    max_speed_mps: float | None = None
    avg_heart_rate: int | None = None
    max_heart_rate: int | None = None
    avg_cadence: float | None = None
    avg_power_w: float | None = None
    normalized_power_w: float | None = None
    total_ascent_m: float | None = None
    total_descent_m: float | None = None
    total_calories: int | None = None

    # 'active' | 'rest' | 'warmup' | 'cooldown' — what makes interval sessions
    # readable ("8 × 400 m" rather than 17 undifferentiated laps).
    intensity: str | None = None
    lap_trigger: str | None = None
    # Which prescribed step this lap belongs to, when the session came from a
    # structured workout. Written by the watch, so the mapping is exact.
    wkt_step_index: int | None = None


@dataclass(slots=True)
class Activity:
    """One FIT `session` message, or a synthetic multisport parent."""

    activity_id: int
    source: Source
    file_hash: str
    start_time_utc: datetime
    start_time_local: datetime

    parent_activity_id: int | None = None
    session_index: int = 0
    garmin_activity_id: int | None = None

    sport: str | None = None
    sub_sport: str | None = None
    name: str | None = None
    tz_offset_seconds: int | None = None

    total_timer_time_s: float | None = None
    total_elapsed_time_s: float | None = None
    total_distance_m: float | None = None
    total_ascent_m: float | None = None
    total_descent_m: float | None = None
    total_calories: int | None = None

    avg_speed_mps: float | None = None
    max_speed_mps: float | None = None
    avg_heart_rate: int | None = None
    max_heart_rate: int | None = None
    avg_cadence: float | None = None
    max_cadence: float | None = None
    avg_power_w: float | None = None
    max_power_w: float | None = None
    normalized_power_w: float | None = None
    intensity_factor: float | None = None
    training_stress_score: float | None = None
    aerobic_training_effect: float | None = None
    anaerobic_training_effect: float | None = None
    avg_temperature_c: float | None = None

    pool_length_m: float | None = None
    total_strokes: int | None = None
    num_laps: int | None = None

    device_product: str | None = None
    device_serial: int | None = None
    start_lat: float | None = None
    start_lon: float | None = None

    extra: dict[str, Any] = field(default_factory=dict)

    laps: list[Lap] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    planned_steps: list[PlannedStep] = field(default_factory=list)
    workout_name: str | None = None

    @property
    def is_multisport_parent(self) -> bool:
        return self.sport == "multisport"

    @property
    def end_time_utc(self) -> datetime | None:
        from datetime import timedelta

        if self.total_elapsed_time_s is None:
            return None
        return self.start_time_utc + timedelta(seconds=self.total_elapsed_time_s)


@dataclass(slots=True)
class ParsedFit:
    """Everything extracted from a single FIT file.

    `activities` holds the multisport parent first (when there is one),
    followed by its child sessions in recording order. A plain run yields a
    single entry with `parent_activity_id is None`.
    """

    file_hash: str
    path: str
    source: Source
    activities: list[Activity] = field(default_factory=list)
    num_sessions: int = 0

    @property
    def is_multisport(self) -> bool:
        """Whether this file is one multisport event.

        Defined by the presence of an aggregating parent, not by the session
        count: a file can legitimately hold several sessions that are separate
        activities — two recordings chained together, or the same one appended
        twice by a transfer tool.
        """
        return self.parent is not None

    @property
    def parent(self) -> Activity | None:
        return next((a for a in self.activities if a.is_multisport_parent), None)

    @property
    def total_records(self) -> int:
        return sum(len(a.records) for a in self.activities)


@dataclass(slots=True)
class ActivityStub:
    """Lightweight activity descriptor returned by a remote source.

    Just enough to decide whether we already have the activity, without
    downloading the FIT file.
    """

    activity_id: int
    start_time_utc: datetime
    sport: str | None = None
    name: str | None = None
    distance_m: float | None = None
    duration_s: float | None = None

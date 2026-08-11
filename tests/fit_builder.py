"""A minimal FIT *encoder*, for tests only.

`fitdecode` reads FIT files but cannot write them, which leaves a test suite
dependent on real recordings — and real recordings are personal data: a GPS
trace starts at the athlete's front door. Committing them to a public
repository is not an option.

So the fixtures are synthesised. This module writes just enough of the FIT
binary format to produce files that `fitdecode` accepts, which buys three
things a donated file would not:

  * the suite is hermetic — clone the repo, run pytest, no data needed;
  * multisport triathlons can be fabricated even though the project owner
    never records one, so that code path is genuinely covered;
  * edge cases (missing GPS, absent heart rate, truncated files) are built on
    purpose instead of waited for.

Format reference: FIT Protocol V2. Layout is
`[14-byte header][definition and data records][2-byte CRC]`. Values are stored
scaled and offset, so encoding applies the inverse of what the parser undoes:
`raw = (value + offset) * scale`.

This is a test helper. It supports the message types the parser reads and
nothing more.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# FIT counts seconds from 1989-12-31 00:00:00 UTC, not the Unix epoch.
FIT_EPOCH_OFFSET = 631065600

# ─── base types ───────────────────────────────────────────────────────
# (type id, byte size, struct format, invalid sentinel)
ENUM = (0x00, 1, "B", 0xFF)
UINT8 = (0x02, 1, "B", 0xFF)
SINT8 = (0x01, 1, "b", 0x7F)
UINT16 = (0x84, 2, "H", 0xFFFF)
SINT16 = (0x83, 2, "h", 0x7FFF)
UINT32 = (0x86, 4, "I", 0xFFFFFFFF)
SINT32 = (0x85, 4, "i", 0x7FFFFFFF)
UINT32Z = (0x8C, 4, "I", 0)

# ─── global message numbers ───────────────────────────────────────────
MSG_FILE_ID = 0
MSG_SESSION = 18
MSG_LAP = 19
MSG_RECORD = 20
MSG_ACTIVITY = 34


@dataclass(frozen=True, slots=True)
class Field:
    """One field in a message definition."""

    name: str
    num: int
    base_type: tuple[int, int, str, int]
    scale: float = 1.0
    offset: float = 0.0

    def encode(self, value: Any) -> bytes:
        _, _size, fmt, invalid = self.base_type
        # Every base type's invalid sentinel fits its own struct format, so a
        # missing value is written as that sentinel rather than omitted.
        if value is None:
            return struct.pack("<" + fmt, invalid)
        if isinstance(value, datetime):
            raw = int(value.timestamp()) - FIT_EPOCH_OFFSET
        else:
            raw = round((value + self.offset) * self.scale)
        return struct.pack("<" + fmt, raw)


# ─── message schemas ──────────────────────────────────────────────────
# Only the fields the parser actually reads. Field numbers come from the FIT
# Profile; changing one silently produces a file that decodes to nonsense.

FILE_ID_FIELDS = [
    Field("type", 0, ENUM),
    Field("manufacturer", 1, UINT16),
    Field("product", 2, UINT16),
    Field("serial_number", 3, UINT32Z),
    Field("time_created", 4, UINT32),
]

ACTIVITY_FIELDS = [
    Field("timestamp", 253, UINT32),
    Field("total_timer_time", 0, UINT32, scale=1000),
    Field("num_sessions", 1, UINT16),
    Field("type", 2, ENUM),
    Field("event", 3, ENUM),
    Field("event_type", 4, ENUM),
    Field("local_timestamp", 5, UINT32),
]

SESSION_FIELDS = [
    Field("message_index", 254, UINT16),
    Field("timestamp", 253, UINT32),
    Field("event", 0, ENUM),
    Field("event_type", 1, ENUM),
    Field("start_time", 2, UINT32),
    Field("start_position_lat", 3, SINT32),
    Field("start_position_long", 4, SINT32),
    Field("sport", 5, ENUM),
    Field("sub_sport", 6, ENUM),
    Field("total_elapsed_time", 7, UINT32, scale=1000),
    Field("total_timer_time", 8, UINT32, scale=1000),
    Field("total_distance", 9, UINT32, scale=100),
    Field("total_calories", 11, UINT16),
    Field("avg_speed", 14, UINT16, scale=1000),
    Field("max_speed", 15, UINT16, scale=1000),
    Field("avg_heart_rate", 16, UINT8),
    Field("max_heart_rate", 17, UINT8),
    Field("avg_cadence", 18, UINT8),
    Field("max_cadence", 19, UINT8),
    Field("avg_power", 20, UINT16),
    Field("max_power", 21, UINT16),
    Field("total_ascent", 22, UINT16),
    Field("total_descent", 23, UINT16),
    Field("total_training_effect", 24, UINT8, scale=10),
    Field("num_laps", 26, UINT16),
    Field("normalized_power", 34, UINT16),
    Field("pool_length", 44, UINT16, scale=100),
    Field("avg_temperature", 57, SINT8),
]

LAP_FIELDS = [
    Field("message_index", 254, UINT16),
    Field("timestamp", 253, UINT32),
    Field("start_time", 2, UINT32),
    Field("total_elapsed_time", 7, UINT32, scale=1000),
    Field("total_timer_time", 8, UINT32, scale=1000),
    Field("total_distance", 9, UINT32, scale=100),
    Field("total_calories", 11, UINT16),
    Field("avg_speed", 13, UINT16, scale=1000),
    Field("max_speed", 14, UINT16, scale=1000),
    Field("avg_heart_rate", 15, UINT8),
    Field("max_heart_rate", 16, UINT8),
    Field("avg_cadence", 17, UINT8),
    Field("avg_power", 19, UINT16),
    Field("total_ascent", 21, UINT16),
    Field("total_descent", 22, UINT16),
    Field("intensity", 23, ENUM),
    Field("lap_trigger", 24, ENUM),
]

RECORD_FIELDS = [
    Field("timestamp", 253, UINT32),
    Field("position_lat", 0, SINT32),
    Field("position_long", 1, SINT32),
    Field("altitude", 2, UINT16, scale=5, offset=500),
    Field("heart_rate", 3, UINT8),
    Field("cadence", 4, UINT8),
    Field("distance", 5, UINT32, scale=100),
    Field("speed", 6, UINT16, scale=1000),
    Field("power", 7, UINT16),
    Field("temperature", 13, SINT8),
]

# ─── enum values used by the fixtures ─────────────────────────────────

# garmin_product code for the Forerunner 955 — the device this project was
# built against. fitdecode resolves it back to the string 'fr955'.
FR955_PRODUCT_ID = 4024
MANUFACTURER_GARMIN = 1

SPORT = {
    "generic": 0, "running": 1, "cycling": 2, "transition": 3,
    "swimming": 5, "training": 10, "multisport": 18,
}
SUB_SPORT = {
    "generic": 0, "treadmill": 1, "trail": 3, "road": 7,
    "indoor_cycling": 6, "lap_swimming": 17, "open_water": 18,
}
INTENSITY = {"active": 0, "rest": 1, "warmup": 2, "cooldown": 3}
LAP_TRIGGER = {"manual": 0, "time": 1, "distance": 2, "session_end": 7}


# ─── CRC ──────────────────────────────────────────────────────────────

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


def fit_crc(data: bytes, crc: int = 0) -> int:
    """FIT's own CRC-16, computed a nibble at a time as the spec describes."""
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]

        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc & 0xFFFF


# ─── writer ───────────────────────────────────────────────────────────


class FitWriter:
    """Accumulates messages, then serialises a valid FIT file.

    Local message types are allocated per global message number, and a
    definition record is emitted the first time each one is used — the same
    scheme a real device follows.
    """

    def __init__(self) -> None:
        self._body = bytearray()
        self._local_types: dict[int, tuple[int, list[Field]]] = {}
        self._next_local = 0

    def _define(self, global_num: int, fields: list[Field]) -> int:
        if global_num in self._local_types:
            return self._local_types[global_num][0]

        if self._next_local > 15:
            raise RuntimeError("out of local message types (max 16)")
        local = self._next_local
        self._next_local += 1
        self._local_types[global_num] = (local, fields)

        # Definition record: header, reserved, architecture (0 = little
        # endian), global message number, field count, then field triplets.
        self._body += struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
        for fld in fields:
            type_id, size, _, _ = fld.base_type
            self._body += struct.pack("<BBB", fld.num, size, type_id)
        return local

    def add(self, global_num: int, fields: list[Field], values: dict[str, Any]) -> None:
        local = self._define(global_num, fields)
        self._body += struct.pack("<B", local)
        for fld in fields:
            self._body += fld.encode(values.get(fld.name))

    def to_bytes(self) -> bytes:
        # 14-byte header: size, protocol version, profile version, data size,
        # ".FIT" tag, then a CRC over the first 12 bytes.
        header = struct.pack("<BBHI4s", 14, 0x20, 2140, len(self._body), b".FIT")
        header += struct.pack("<H", fit_crc(header))
        payload = header + bytes(self._body)
        return payload + struct.pack("<H", fit_crc(payload))

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.to_bytes())
        return path


# ─── high-level fixture helpers ───────────────────────────────────────


def _deg_to_semicircles(degrees: float) -> int:
    return int(degrees * (2**31) / 180.0)


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


def build_run(
    *,
    start: datetime | None = None,
    duration_s: int = 1800,
    distance_m: float = 6000.0,
    tz_offset_s: int = 7200,
    num_records: int = 60,
    with_gps: bool = True,
    with_hr: bool = True,
    laps: int = 2,
    with_activity_msg: bool = True,
    manufacturer: int = MANUFACTURER_GARMIN,
    product: int = FR955_PRODUCT_ID,
    longitude: float = 4.85,
) -> bytes:
    """A single-session run — the shape of 95% of real files.

    The optional arguments reproduce behaviours observed across a corpus of
    real devices: watches that omit the `activity` message entirely, and
    manufacturers whose product code the FIT profile cannot resolve to a name.
    """
    start = start or _dt("2026-03-15T07:30:00")
    end_ts = start.timestamp() + duration_s
    speed = distance_m / duration_s

    writer = FitWriter()
    writer.add(MSG_FILE_ID, FILE_ID_FIELDS, {
        "type": 4, "manufacturer": manufacturer, "product": product,
        "serial_number": 3987654321, "time_created": start,
    })

    step = max(duration_s // num_records, 1)
    for i in range(num_records):
        elapsed = i * step
        ts = datetime.fromtimestamp(start.timestamp() + elapsed, tz=UTC)
        writer.add(MSG_RECORD, RECORD_FIELDS, {
            "timestamp": ts,
            "position_lat": _deg_to_semicircles(45.75 + i * 0.0001) if with_gps else None,
            "position_long": _deg_to_semicircles(longitude + i * 0.0001) if with_gps else None,
            "altitude": 170.0 + (i % 20),
            "heart_rate": 140 + (i % 25) if with_hr else None,
            "cadence": 85 + (i % 4),          # per-leg; parser doubles it
            "distance": speed * elapsed,
            "speed": speed,
            "power": 260 + (i % 30),
            "temperature": 14,
        })

    lap_duration = duration_s / laps
    for lap_index in range(laps):
        lap_start = datetime.fromtimestamp(start.timestamp() + lap_index * lap_duration, tz=UTC)
        writer.add(MSG_LAP, LAP_FIELDS, {
            "message_index": lap_index,
            "timestamp": datetime.fromtimestamp(
                start.timestamp() + (lap_index + 1) * lap_duration, tz=UTC
            ),
            "start_time": lap_start,
            "total_elapsed_time": lap_duration,
            "total_timer_time": lap_duration,
            "total_distance": distance_m / laps,
            "total_calories": 150,
            "avg_speed": speed,
            "max_speed": speed * 1.2,
            "avg_heart_rate": 150 if with_hr else None,
            "max_heart_rate": 168 if with_hr else None,
            "avg_cadence": 86,
            "avg_power": 265,
            "total_ascent": 20,
            "total_descent": 18,
            "intensity": INTENSITY["active"],
            "lap_trigger": LAP_TRIGGER["distance"],
        })

    writer.add(MSG_SESSION, SESSION_FIELDS, {
        "message_index": 0,
        "timestamp": datetime.fromtimestamp(end_ts, tz=UTC),
        "start_time": start,
        "start_position_lat": _deg_to_semicircles(45.75) if with_gps else None,
        "start_position_long": _deg_to_semicircles(longitude) if with_gps else None,
        "sport": SPORT["running"],
        "sub_sport": SUB_SPORT["road"],
        "total_elapsed_time": duration_s,
        "total_timer_time": duration_s,
        "total_distance": distance_m,
        "total_calories": 420,
        "avg_speed": speed,
        "max_speed": speed * 1.25,
        "avg_heart_rate": 152 if with_hr else None,
        "max_heart_rate": 171 if with_hr else None,
        "avg_cadence": 86,
        "max_cadence": 92,
        "avg_power": 265,
        "max_power": 340,
        "total_ascent": 42,
        "total_descent": 40,
        "total_training_effect": 3.4,
        "num_laps": laps,
        "normalized_power": 272,
        "avg_temperature": 14,
    })

    if with_activity_msg:
        writer.add(MSG_ACTIVITY, ACTIVITY_FIELDS, {
            "timestamp": datetime.fromtimestamp(end_ts, tz=UTC),
            "total_timer_time": duration_s,
            "num_sessions": 1,
            "type": 0,
            "event": 26,
            "event_type": 1,
            # local_timestamp is the same instant expressed in local time.
            "local_timestamp": datetime.fromtimestamp(end_ts + tz_offset_s, tz=UTC),
        })
    return writer.to_bytes()


def build_concatenated(count: int = 2, **kwargs: Any) -> bytes:
    """The same recording appended to itself.

    Some transfer tools chain FIT files end to end, producing a stream with
    several headers and several identical `activity` messages. Observed in the
    wild; without deduplication one ride is stored twice and every weekly
    total is inflated.
    """
    return b"".join(build_run(**kwargs) for _ in range(count))


def build_chained_distinct(**kwargs: Any) -> bytes:
    """Two unrelated recordings in one stream, days apart.

    Two sessions in one file, but two `activity` messages — this is not a
    triathlon, and must not be collapsed into one.
    """
    first = build_run(start=_dt("2026-03-15T07:30:00"), **kwargs)
    second = build_run(start=_dt("2026-03-18T18:00:00"), **kwargs)
    return first + second


# Olympic-distance triathlon: swim, T1, bike, T2, run.
_TRI_LEGS = [
    ("swimming", "open_water", 1500.0, 1650),
    ("transition", "generic", 0.0, 150),
    ("cycling", "road", 40000.0, 4200),
    ("transition", "generic", 0.0, 90),
    ("running", "road", 10000.0, 2700),
]


def build_triathlon(
    *,
    start: datetime | None = None,
    tz_offset_s: int = 7200,
    records_per_leg: int = 20,
) -> bytes:
    """A multisport file: five sessions in one file, transitions included.

    This is the shape the project owner never records but any triathlete
    cloning the repo will, so the parser has to get it right.
    """
    start = start or _dt("2026-06-15T08:00:00")
    writer = FitWriter()
    writer.add(MSG_FILE_ID, FILE_ID_FIELDS, {
        "type": 4, "manufacturer": 1, "product": FR955_PRODUCT_ID,
        "serial_number": 3987654321, "time_created": start,
    })

    cursor = start.timestamp()
    sessions: list[dict[str, Any]] = []

    for lap_index, (sport, sub_sport, distance_m, duration_s) in enumerate(_TRI_LEGS):
        session_index = lap_index
        leg_start = datetime.fromtimestamp(cursor, tz=UTC)
        speed = distance_m / duration_s if distance_m else 0.0

        step = max(duration_s // records_per_leg, 1)
        for i in range(records_per_leg):
            elapsed = i * step
            writer.add(MSG_RECORD, RECORD_FIELDS, {
                "timestamp": datetime.fromtimestamp(cursor + elapsed, tz=UTC),
                "position_lat": _deg_to_semicircles(43.60 + i * 0.0002),
                "position_long": _deg_to_semicircles(1.44 + i * 0.0002),
                "altitude": 150.0,
                "heart_rate": {"swimming": 145, "transition": 130, "cycling": 152,
                               "running": 165}[sport],
                "cadence": 80,
                "distance": speed * elapsed,
                "speed": speed,
                "power": 240 if sport == "cycling" else None,
                "temperature": 22,
            })

        writer.add(MSG_LAP, LAP_FIELDS, {
            "message_index": lap_index,
            "timestamp": datetime.fromtimestamp(cursor + duration_s, tz=UTC),
            "start_time": leg_start,
            "total_elapsed_time": duration_s,
            "total_timer_time": duration_s,
            "total_distance": distance_m,
            "total_calories": 200,
            "avg_speed": speed,
            "max_speed": speed * 1.1 if speed else None,
            "avg_heart_rate": 150,
            "max_heart_rate": 170,
            "intensity": INTENSITY["active"],
            "lap_trigger": LAP_TRIGGER["session_end"],
        })
        sessions.append({
            "message_index": session_index,
            "timestamp": datetime.fromtimestamp(cursor + duration_s, tz=UTC),
            "start_time": leg_start,
            "start_position_lat": _deg_to_semicircles(43.60),
            "start_position_long": _deg_to_semicircles(1.44),
            "sport": SPORT[sport],
            "sub_sport": SUB_SPORT[sub_sport],
            "total_elapsed_time": duration_s,
            "total_timer_time": duration_s,
            "total_distance": distance_m,
            "total_calories": 200,
            "avg_speed": speed,
            "max_speed": speed * 1.1 if speed else None,
            "avg_heart_rate": {"swimming": 145, "transition": 130, "cycling": 152,
                               "running": 165}[sport],
            "max_heart_rate": 175,
            "num_laps": 1,
        })
        cursor += duration_s

    # Real devices emit every session message after all of its data.
    for session in sessions:
        writer.add(MSG_SESSION, SESSION_FIELDS, session)

    total = cursor - start.timestamp()
    writer.add(MSG_ACTIVITY, ACTIVITY_FIELDS, {
        "timestamp": datetime.fromtimestamp(cursor, tz=UTC),
        "total_timer_time": total,
        "num_sessions": len(_TRI_LEGS),
        "type": 0,
        "event": 26,
        "event_type": 1,
        "local_timestamp": datetime.fromtimestamp(cursor + tz_offset_s, tz=UTC),
    })
    return writer.to_bytes()


def build_without_session() -> bytes:
    """A valid FIT file that is not an activity — must be rejected cleanly."""
    writer = FitWriter()
    writer.add(MSG_FILE_ID, FILE_ID_FIELDS, {
        "type": 2, "manufacturer": 1, "product": FR955_PRODUCT_ID,
        "serial_number": 1, "time_created": _dt("2026-01-01T00:00:00"),
    })
    return writer.to_bytes()


def build_corrupt() -> bytes:
    """Valid header, truncated body — the shape of an interrupted download."""
    payload = bytearray(build_run(num_records=10))
    return bytes(payload[: len(payload) // 2])

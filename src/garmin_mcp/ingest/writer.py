"""Writing parsed activities into DuckDB.

Two properties matter here and everything else follows from them.

**Idempotency.** Ingesting the same file twice must change nothing. The same
activity can legitimately arrive twice — pulled from Garmin, then dropped into
the inbox by hand months later when the auth breaks. Rather than trying to
merge, each activity is deleted and rewritten inside one transaction:
`DELETE` then `INSERT`, never `UPDATE`. It is simpler to reason about, and it
means a parser improvement can be applied by re-ingesting with no cleanup.

**Short write windows.** DuckDB gives exclusive access to a single writer, and
the MCP server cannot read while this holds the file. Writes are therefore
batched per file and the connection is released immediately after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from garmin_mcp.domain.models import Activity, ParsedFit
from garmin_mcp.errors import IngestError
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

# Column order is duplicated from schema.sql on purpose: an INSERT that relies
# on table order breaks silently when a column is added in the middle.
_ACTIVITY_COLUMNS = (
    "activity_id",
    "parent_activity_id",
    "session_index",
    "source",
    "file_hash",
    "garmin_activity_id",
    "sport",
    "sub_sport",
    "name",
    "start_time_utc",
    "start_time_local",
    "tz_offset_seconds",
    "total_timer_time_s",
    "total_elapsed_time_s",
    "total_distance_m",
    "total_ascent_m",
    "total_descent_m",
    "total_calories",
    "avg_speed_mps",
    "max_speed_mps",
    "avg_heart_rate",
    "max_heart_rate",
    "avg_cadence",
    "max_cadence",
    "avg_power_w",
    "max_power_w",
    "normalized_power_w",
    "intensity_factor",
    "training_stress_score",
    "aerobic_training_effect",
    "anaerobic_training_effect",
    "avg_temperature_c",
    "pool_length_m",
    "total_strokes",
    "num_laps",
    "device_product",
    "device_serial",
    "start_lat",
    "start_lon",
    "ingested_at",
    "extra",
)

_LAP_COLUMNS = (
    "activity_id",
    "lap_index",
    "start_time_utc",
    "total_timer_time_s",
    "total_distance_m",
    "avg_speed_mps",
    "max_speed_mps",
    "avg_heart_rate",
    "max_heart_rate",
    "avg_cadence",
    "avg_power_w",
    "normalized_power_w",
    "total_ascent_m",
    "total_descent_m",
    "total_calories",
    "intensity",
    "lap_trigger",
    "wkt_step_index",
)

_PLANNED_STEP_COLUMNS = (
    "activity_id",
    "step_index",
    "workout_name",
    "intensity",
    "duration_type",
    "duration_value",
    "target_type",
    "target_low",
    "target_high",
    "repeat_from_step",
    "repeat_count",
)

_RECORD_COLUMNS = (
    "activity_id",
    "ts",
    "elapsed_s",
    "lap_index",
    "lat",
    "lon",
    "altitude_m",
    "distance_m",
    "speed_mps",
    "heart_rate",
    "cadence",
    "power_w",
    "temperature_c",
    "grade",
    "vertical_oscillation_mm",
    "vertical_ratio",
    "stance_time_ms",
    "stance_time_percent",
    "step_length_mm",
    "left_right_balance",
    "respiration_rate",
    "accumulated_power_w",
    "extra",
)


@dataclass(frozen=True, slots=True)
class WriteResult:
    file_hash: str
    activities_written: int
    laps_written: int
    records_written: int
    replaced: bool

    @property
    def is_noop(self) -> bool:
        return self.activities_written == 0


def _placeholders(columns: tuple[str, ...]) -> str:
    return ", ".join("?" * len(columns))


def _as_json(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value, default=str) if value else None


def _activity_row(activity: Activity, ingested_at: datetime) -> tuple[Any, ...]:
    return (
        activity.activity_id,
        activity.parent_activity_id,
        activity.session_index,
        activity.source,
        activity.file_hash,
        activity.garmin_activity_id,
        activity.sport,
        activity.sub_sport,
        activity.name,
        activity.start_time_utc,
        activity.start_time_local,
        activity.tz_offset_seconds,
        activity.total_timer_time_s,
        activity.total_elapsed_time_s,
        activity.total_distance_m,
        activity.total_ascent_m,
        activity.total_descent_m,
        activity.total_calories,
        activity.avg_speed_mps,
        activity.max_speed_mps,
        activity.avg_heart_rate,
        activity.max_heart_rate,
        activity.avg_cadence,
        activity.max_cadence,
        activity.avg_power_w,
        activity.max_power_w,
        activity.normalized_power_w,
        activity.intensity_factor,
        activity.training_stress_score,
        activity.aerobic_training_effect,
        activity.anaerobic_training_effect,
        activity.avg_temperature_c,
        activity.pool_length_m,
        activity.total_strokes,
        activity.num_laps,
        activity.device_product,
        activity.device_serial,
        activity.start_lat,
        activity.start_lon,
        ingested_at,
        _as_json(activity.extra),
    )


def _lap_rows(activity: Activity) -> list[tuple[Any, ...]]:
    return [
        (
            activity.activity_id,
            lap.lap_index,
            lap.start_time_utc,
            lap.total_timer_time_s,
            lap.total_distance_m,
            lap.avg_speed_mps,
            lap.max_speed_mps,
            lap.avg_heart_rate,
            lap.max_heart_rate,
            lap.avg_cadence,
            lap.avg_power_w,
            lap.normalized_power_w,
            lap.total_ascent_m,
            lap.total_descent_m,
            lap.total_calories,
            lap.intensity,
            lap.lap_trigger,
            lap.wkt_step_index,
        )
        for lap in activity.laps
    ]


def _planned_step_rows(activity: Activity) -> list[tuple[Any, ...]]:
    return [
        (
            activity.activity_id,
            step.step_index,
            step.name,
            step.intensity,
            step.duration_type,
            step.duration_value,
            step.target_type,
            step.target_low,
            step.target_high,
            step.repeat_from_step,
            step.repeat_count,
        )
        for step in activity.planned_steps
    ]


def _record_rows(activity: Activity) -> list[tuple[Any, ...]]:
    return [
        (
            activity.activity_id,
            record.ts,
            record.elapsed_s,
            record.lap_index,
            record.lat,
            record.lon,
            record.altitude_m,
            record.distance_m,
            record.speed_mps,
            record.heart_rate,
            record.cadence,
            record.power_w,
            record.temperature_c,
            record.grade,
            record.vertical_oscillation_mm,
            record.vertical_ratio,
            record.stance_time_ms,
            record.stance_time_percent,
            record.step_length_mm,
            record.left_right_balance,
            record.respiration_rate,
            record.accumulated_power_w,
            _as_json(record.extra),
        )
        for record in activity.records
    ]


def file_is_ingested(
    conn: duckdb.DuckDBPyConnection,
    file_hash: str,
    parser_version: int,
) -> bool:
    """Whether this exact file has already been parsed by this parser version.

    Keyed on content, so the same activity arriving from Garmin and from the
    inbox is recognised as one file regardless of its name. A parser version
    bump makes this return False, which is how a re-parse gets triggered
    without re-downloading anything.
    """
    row = conn.execute(
        """
        SELECT 1 FROM files
        WHERE file_hash = ? AND status = 'parsed' AND parser_version >= ?
        """,
        [file_hash, parser_version],
    ).fetchone()
    return row is not None


def record_file_failure(
    conn: duckdb.DuckDBPyConnection,
    *,
    file_hash: str,
    path: str,
    source: str,
    parser_version: int,
    error: str,
    size_bytes: int | None = None,
) -> None:
    """Remember that a file could not be parsed, so it is not retried forever."""
    conn.execute(
        """
        INSERT INTO files (file_hash, path, source, bytes, downloaded_at,
                           parsed_at, parser_version, status, error)
        VALUES (?, ?, ?, ?, ?, NULL, ?, 'failed', ?)
        ON CONFLICT (file_hash) DO UPDATE SET
            path = excluded.path,
            status = 'failed',
            error = excluded.error,
            parser_version = excluded.parser_version
        """,
        [file_hash, path, source, size_bytes, datetime.now(UTC), parser_version, error[:2000]],
    )


def write_parsed(
    conn: duckdb.DuckDBPyConnection,
    parsed: ParsedFit,
    *,
    parser_version: int,
    size_bytes: int | None = None,
    downloaded_at: datetime | None = None,
) -> WriteResult:
    """Persist one parsed file. Idempotent: re-running replaces, never duplicates.

    Everything happens in a single transaction. A crash halfway through leaves
    the database exactly as it was, rather than with an activity whose samples
    are half written.
    """
    now = datetime.now(UTC)
    activity_ids = [a.activity_id for a in parsed.activities]

    try:
        conn.execute("BEGIN TRANSACTION")

        existing = conn.execute(
            "SELECT count(*) FROM activities WHERE activity_id IN "
            f"({', '.join('?' * len(activity_ids))})",
            activity_ids,
        ).fetchone()
        replaced = bool(existing and existing[0])

        conn.execute(
            """
            INSERT INTO files (file_hash, path, source, bytes, downloaded_at,
                               parsed_at, parser_version, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', NULL)
            ON CONFLICT (file_hash) DO UPDATE SET
                path = excluded.path,
                bytes = excluded.bytes,
                parsed_at = excluded.parsed_at,
                parser_version = excluded.parser_version,
                status = 'parsed',
                error = NULL
            """,
            [
                parsed.file_hash,
                parsed.path,
                parsed.source,
                size_bytes,
                downloaded_at or now,
                now,
                parser_version,
            ],
        )

        # Replace rather than merge. Children are removed too: a re-parse may
        # split a file into a different number of sessions, and stale legs
        # pointing at a vanished parent would silently distort every total.
        for table in ("records", "laps", "workout_steps", "activities"):
            conn.execute(
                f"DELETE FROM {table} WHERE activity_id IN ({', '.join('?' * len(activity_ids))})",
                activity_ids,
            )
        conn.execute(
            "DELETE FROM activities WHERE file_hash = ? AND activity_id NOT IN "
            f"({', '.join('?' * len(activity_ids))})",
            [parsed.file_hash, *activity_ids],
        )

        conn.executemany(
            f"INSERT INTO activities ({', '.join(_ACTIVITY_COLUMNS)}) "
            f"VALUES ({_placeholders(_ACTIVITY_COLUMNS)})",
            [_activity_row(a, now) for a in parsed.activities],
        )

        lap_rows = [row for a in parsed.activities for row in _lap_rows(a)]
        if lap_rows:
            conn.executemany(
                f"INSERT INTO laps ({', '.join(_LAP_COLUMNS)}) "
                f"VALUES ({_placeholders(_LAP_COLUMNS)})",
                lap_rows,
            )

        planned_rows = [row for a in parsed.activities for row in _planned_step_rows(a)]
        if planned_rows:
            conn.executemany(
                f"INSERT INTO workout_steps ({', '.join(_PLANNED_STEP_COLUMNS)}) "
                f"VALUES ({_placeholders(_PLANNED_STEP_COLUMNS)})",
                planned_rows,
            )

        record_rows = [row for a in parsed.activities for row in _record_rows(a)]
        if record_rows:
            conn.executemany(
                f"INSERT INTO records ({', '.join(_RECORD_COLUMNS)}) "
                f"VALUES ({_placeholders(_RECORD_COLUMNS)})",
                record_rows,
            )

        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        raise IngestError(f"failed to write {parsed.path}: {exc}") from exc

    result = WriteResult(
        file_hash=parsed.file_hash,
        activities_written=len(parsed.activities),
        laps_written=len(lap_rows),
        records_written=len(record_rows),
        replaced=replaced,
    )
    log.info(
        "db.written",
        file_hash=parsed.file_hash[:12],
        activities=result.activities_written,
        laps=result.laps_written,
        records=result.records_written,
        replaced=replaced,
    )
    return result

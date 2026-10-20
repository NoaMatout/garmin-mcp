"""Writing courses into DuckDB.

The summary row is upserted (`ON CONFLICT ... DO UPDATE`), same reasoning as
`sleep_writer`: one row, one natural key, no fan-out to keep consistent.
Track points and waypoints are DELETE-then-INSERT instead, `ingest/writer.py`'s
rule for anything that fans out from a parent — a course fetched again could
legitimately have a different number of points if the athlete edited it in
Garmin Connect, and a stale tail from a shorter previous version must not
survive that.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import duckdb

from garmin_mcp.domain.models import Course
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

_COURSE_COLUMNS = (
    "course_id",
    "name",
    "sport",
    "distance_m",
    "elevation_gain_m",
    "elevation_loss_m",
    "start_lat",
    "start_lon",
    "created_at",
    "updated_at",
    "favorite",
    "ingested_at",
    "extra",
)


def _course_row(course: Course, ingested_at: datetime) -> tuple[Any, ...]:
    return (
        course.course_id,
        course.name,
        course.sport,
        course.distance_m,
        course.elevation_gain_m,
        course.elevation_loss_m,
        course.start_lat,
        course.start_lon,
        course.created_at,
        course.updated_at,
        course.favorite,
        ingested_at,
        json.dumps(course.extra, default=str) if course.extra else None,
    )


def write_course_summary(conn: duckdb.DuckDBPyConnection, course: Course) -> None:
    """Upsert the summary row only — cheap, safe to call every cycle."""
    updates = ", ".join(f"{column} = excluded.{column}" for column in _COURSE_COLUMNS[1:])
    conn.execute(
        f"""
        INSERT INTO courses ({", ".join(_COURSE_COLUMNS)})
        VALUES ({", ".join("?" * len(_COURSE_COLUMNS))})
        ON CONFLICT (course_id) DO UPDATE SET {updates}
        """,
        _course_row(course, datetime.now(UTC)),
    )


def write_course_detail(conn: duckdb.DuckDBPyConnection, course: Course) -> None:
    """Replace a course's track and waypoints. Call after `attach_detail`.

    Does not touch the summary row — `sync_courses_from_garmin` writes that
    separately, on every cycle, from the cheap listing alone.
    """
    conn.execute("DELETE FROM course_points WHERE course_id = ?", [course.course_id])
    conn.execute("DELETE FROM course_waypoints WHERE course_id = ?", [course.course_id])

    if course.track:
        conn.executemany(
            """
            INSERT INTO course_points
                (course_id, point_index, lat, lon, elevation_m, distance_m)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (course.course_id, p.point_index, p.lat, p.lon, p.elevation_m, p.distance_m)
                for p in course.track
            ],
        )

    if course.waypoints:
        conn.executemany(
            """
            INSERT INTO course_waypoints
                (course_id, waypoint_index, name, point_type, distance_m, lat, lon, elevation_m)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    course.course_id,
                    w.waypoint_index,
                    w.name,
                    w.point_type,
                    w.distance_m,
                    w.lat,
                    w.lon,
                    w.elevation_m,
                )
                for w in course.waypoints
            ],
        )

    log.info(
        "course.detail_written",
        course_id=course.course_id,
        points=len(course.track),
        waypoints=len(course.waypoints),
    )

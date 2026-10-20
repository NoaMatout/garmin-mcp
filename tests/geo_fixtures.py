"""Synthetic activities for the location tests.

Rows are inserted straight into the tables rather than built as FIT files and
ingested: these tests are about what the tools do with stored GPS samples, and
a FIT round trip would add a parser between the fixture and the thing under
test. Every coordinate here is invented; nothing is derived from a real trace.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import duckdb

# Fictional anchor near Reims, so distances in the tests read naturally.
REIMS = (49.2583, 4.0317)
KM_LAT = 1 / 111.195  # degrees of latitude per km


@dataclass(frozen=True)
class Fix:
    """One recorded sample. `lat`/`lon` of None model a sample without a fix."""

    elapsed_s: float
    lat: float | None
    lon: float | None
    alt: float | None = 170.0
    dist_m: float | None = None


def line(
    n: int,
    *,
    lat: float = REIMS[0],
    lon: float = REIMS[1],
    d_lat: float = 0.0001,
    d_lon: float = 0.0,
    dt: float = 1.0,
    t0: float = 0.0,
    alt: float | None = 170.0,
    step_m: float = 10.0,
) -> list[Fix]:
    """A straight track of `n` samples, one every `dt` seconds."""
    return [
        Fix(
            t0 + i * dt,
            lat + i * d_lat,
            lon + i * d_lon,
            None if alt is None else alt + (i % 7),
            i * step_m,
        )
        for i in range(n)
    ]


def loop(
    laps: int,
    *,
    points_per_lap: int = 60,
    centre: tuple[float, float] = REIMS,
    radius_km: float = 0.3,
    dt: float = 5.0,
) -> list[Fix]:
    """A circular track run `laps` times over exactly the same coordinates."""
    fixes = []
    for i in range(laps * points_per_lap + 1):
        angle = 2 * math.pi * (i % points_per_lap) / points_per_lap
        lat = centre[0] + radius_km * KM_LAT * math.sin(angle)
        lon = centre[1] + radius_km * KM_LAT / math.cos(math.radians(centre[0])) * math.cos(angle)
        fixes.append(Fix(i * dt, lat, lon, 170.0 + (i % 5), i * 8.0))
    return fixes


def seed_activity(
    conn: duckdb.DuckDBPyConnection,
    activity_id: int,
    fixes: Sequence[Fix] | None,
    *,
    start: tuple[float, float] | None = REIMS,
    sport: str = "running",
    sub_sport: str | None = "trail",
    name: str | None = None,
    distance_m: float | None = 10_000.0,
    ascent_m: float | None = 300.0,
    descent_m: float | None = 290.0,
    timer_s: float | None = 3600.0,
    elapsed_s: float | None = 3900.0,
    day: int = 1,
    parent_activity_id: int | None = None,
) -> None:
    """Insert one activity (and its samples) the way the ingest pipeline would."""
    file_hash = f"hash-{activity_id}"
    started = datetime(2026, 9, day, 7, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO files (file_hash, path, source, parser_version, status) "
        "VALUES (?, ?, 'garmin', 1, 'parsed')",
        [file_hash, f"{activity_id}.fit"],
    )
    conn.execute(
        """
        INSERT INTO activities (
            activity_id, parent_activity_id, source, file_hash, sport, sub_sport, name,
            start_time_utc, start_time_local, total_timer_time_s, total_elapsed_time_s,
            total_distance_m, total_ascent_m, total_descent_m, start_lat, start_lon,
            ingested_at
        ) VALUES (?, ?, 'garmin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            activity_id,
            parent_activity_id,
            file_hash,
            sport,
            sub_sport,
            name or f"Run {activity_id}",
            started,
            started.replace(tzinfo=None) + timedelta(hours=2),
            timer_s,
            elapsed_s,
            distance_m,
            ascent_m,
            descent_m,
            start[0] if start else None,
            start[1] if start else None,
            started,
        ],
    )
    for fix in fixes or []:
        conn.execute(
            "INSERT INTO records (activity_id, ts, elapsed_s, lat, lon, altitude_m, distance_m) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                activity_id,
                started + timedelta(seconds=fix.elapsed_s),
                fix.elapsed_s,
                fix.lat,
                fix.lon,
                fix.alt,
                fix.dist_m,
            ],
        )

"""Reading a recorded track back out of the database.

The one place that turns stored samples into geometry, shared by the MCP tools,
the GPX resource and the CLI export so they cannot disagree about what "the
track of activity N" is.

Everything is read from `records` at the resolution it was ingested — one row
per recorded sample, nothing averaged. Simplification, where a caller asks for
it, happens afterwards and says so; the full-resolution track is what GPX
export uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Any

import duckdb

from garmin_mcp.db import queries
from garmin_mcp.domain import gpx
from garmin_mcp.domain.geo import (
    GAP_THRESHOLD_S,
    Interruption,
    TrackPoint,
    is_valid_fix,
    split_segments,
)
from garmin_mcp.errors import ActivityNotFoundError, RouteUnavailableError

# Hard ceiling on a track read into memory. 150 000 fixes is 41 hours at one
# sample per second — beyond any event in scope — so hitting it means something
# other than a normal recording, and failing is better than a truncated route.
MAX_ROUTE_FIXES = 150_000

# GPS quality thresholds. Named so the tools can state them back to the caller.
MIN_GPS_COVERAGE = 0.95
EDGE_GAP_S = 60.0


@dataclass(slots=True)
class ActivityRoute:
    """A recorded track as stored: full resolution, interruptions kept apart."""

    activity: dict[str, Any]
    segments: list[list[TrackPoint]]
    interruptions: list[Interruption]
    samples_total: int
    gap_threshold_s: float = GAP_THRESHOLD_S

    @property
    def fix_count(self) -> int:
        return sum(len(s) for s in self.segments)

    @property
    def altitude_count(self) -> int:
        return sum(1 for s in self.segments for p in s if p.altitude_m is not None)

    @property
    def distance_count(self) -> int:
        return sum(1 for s in self.segments for p in s if p.distance_m is not None)


def load_activity_route(
    conn: duckdb.DuckDBPyConnection,
    activity_id: int,
    *,
    gap_threshold_s: float = GAP_THRESHOLD_S,
) -> ActivityRoute:
    """Every valid fix of an activity, cut into segments at recording gaps."""
    activity = queries.get_activity(conn, activity_id)
    if activity is None:
        raise ActivityNotFoundError(activity_id)

    samples = queries.count_records(conn, activity_id)
    fixes = queries.count_route_fixes(conn, activity_id)
    if fixes > MAX_ROUTE_FIXES:
        raise RouteUnavailableError(
            f"activity {activity_id} has {fixes} GPS fixes, above the {MAX_ROUTE_FIXES} limit",
            hint="this is far longer than any supported event; check the activity",
        )

    rows = queries.get_route_fixes(conn, activity_id, limit=MAX_ROUTE_FIXES)
    points = [
        TrackPoint(
            lat=float(lat),
            lon=float(lon),
            elapsed_s=float(elapsed),
            altitude_m=float(altitude) if altitude is not None else None,
            distance_m=float(distance) if distance is not None else None,
            time_iso=ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else None,
        )
        for ts, elapsed, lat, lon, altitude, distance in rows
        # The SQL already filters; this keeps the two definitions honest.
        if is_valid_fix(lat, lon)
    ]
    segments, interruptions = split_segments(points, gap_threshold_s)
    return ActivityRoute(activity, segments, interruptions, samples, gap_threshold_s)


def assess_quality(
    stats: dict[str, Any] | None, gap_threshold_s: float = GAP_THRESHOLD_S
) -> dict[str, Any]:
    """Turn the raw GPS evidence for one activity into indicators and reasons.

    `stats` is a row from `queries.gps_quality`, or None for an activity with
    no stored samples (a summary-only import). Every flag comes with the number
    behind it so the reader can disagree with the threshold.
    """
    if stats is None or not stats["samples"]:
        return {
            "has_gps": False,
            "has_altitude": False,
            "samples": 0,
            "gps_points": 0,
            "partial_track": None,
            "partial_reasons": [],
            "note": "no per-second samples stored for this activity: only its summary",
        }

    samples = int(stats["samples"])
    gps = int(stats["gps_points"])
    altitude = int(stats["altitude_points"])
    has_gps = gps > 0

    quality: dict[str, Any] = {
        "has_gps": has_gps,
        "has_altitude": altitude > 0,
        "samples": samples,
        "gps_points": gps,
    }
    if not has_gps:
        quality["partial_track"] = None
        quality["partial_reasons"] = []
        quality["note"] = "samples are stored but none carries a valid GPS fix"
        return quality

    coverage = gps / samples
    interruptions = int(stats["interruptions"])
    reasons: list[str] = []
    if coverage < MIN_GPS_COVERAGE:
        reasons.append(f"only {coverage:.0%} of samples carry a GPS fix")
    if interruptions:
        reasons.append(f"{interruptions} recording gap(s) longer than {gap_threshold_s:.0f} s")
    if stats["first_fix_s"] is not None and stats["first_fix_s"] > EDGE_GAP_S:
        reasons.append(f"first fix only {stats['first_fix_s']:.0f} s after the start")
    tail = float(stats["last_sample_s"] or 0.0) - float(stats["last_fix_s"] or 0.0)
    if tail > EDGE_GAP_S:
        reasons.append(f"no fix during the last {tail:.0f} s")

    quality.update(
        {
            "gps_coverage": round(coverage, 3),
            "altitude_coverage": round(altitude / gps, 3),
            "interruptions": interruptions,
            "longest_gap_s": round(float(stats["longest_gap_s"]), 1)
            if stats["longest_gap_s"] is not None and interruptions
            else None,
            "partial_track": bool(reasons),
            "partial_reasons": reasons,
        }
    )
    return quality


def activity_gpx(route: ActivityRoute) -> str:
    """The whole recorded track as GPX, one `<trkseg>` per uninterrupted segment."""
    name = route.activity.get("name") or f"Activity {route.activity['activity_id']}"
    return gpx.build_gpx(
        name,
        route.segments,
        description="Exported by garmin-mcp from a recorded activity. "
        "A recording is not a validated or public route.",
    )


def course_gpx(conn: duckdb.DuckDBPyConnection, course_id: int) -> str:
    """A saved course's stored geometry as GPX (a single segment, no times)."""
    course = queries.get_course(conn, course_id)
    if course is None:
        raise RouteUnavailableError(
            f"course {course_id} not found", hint="check the id with list_courses"
        )
    rows = queries.get_course_fixes(conn, course_id)
    if not rows:
        raise RouteUnavailableError(
            f"course {course_id} has no geometry stored yet",
            hint="geometry is fetched on the next sync cycle; try again after sync_now",
        )
    segment = [
        TrackPoint(
            lat=float(lat),
            lon=float(lon),
            elapsed_s=0.0,
            altitude_m=float(elevation) if elevation is not None else None,
            distance_m=float(distance) if distance is not None else None,
        )
        for lat, lon, elevation, distance in rows
        if is_valid_fix(lat, lon)
    ]
    return gpx.build_gpx(
        course.get("name") or f"Course {course_id}",
        [segment],
        description="Exported by garmin-mcp from a saved Garmin course.",
    )

"""Finding past runs by place, and reading their tracks back.

Two tools and two resources, all read-only and all answered from the local
database, so they keep working when Garmin is unreachable or the session has
expired.

* `find_local_activities` — recorded runs that *started* near a point.
* `get_activity_route` — one run's track: simplified for a map, with a pointer
  to the full-resolution GPX.
* `garmin://activity/{id}/route.gpx`, `garmin://course/{id}/route.gpx` — the
  GPX itself, as MCP resources.

What none of this claims: a recorded activity is evidence that someone ran
there once. It is not a validated route, not public, and says nothing about
trail access rights, technicality or water points.
"""

from __future__ import annotations

import hashlib
from typing import Any

from garmin_mcp import routes
from garmin_mcp.config import Settings, get_settings
from garmin_mcp.db import queries
from garmin_mcp.db.connection import reading
from garmin_mcp.domain.geo import GAP_THRESHOLD_S, simplify_segments
from garmin_mcp.domain.gpx import MIME_TYPE as GPX_MIME_TYPE
from garmin_mcp.domain.units import format_duration
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

DEFAULT_FIND_LIMIT = 20
MAX_FIND_LIMIT = 100
MAX_RADIUS_KM = 300.0
DEFAULT_ROUTE_POINTS = 500
MAX_ROUTE_POINTS = 2000

ACTIVITY_GPX_URI = "garmin://activity/{activity_id}/route.gpx"
COURSE_GPX_URI = "garmin://course/{course_id}/route.gpx"

_TRACK_DISCLAIMER = (
    "A recorded activity shows where someone ran once. It is not a validated or "
    "public route, and says nothing about trail access rights, technicality, "
    "water points or whether the path still exists."
)


def _settings() -> Settings:
    return get_settings()


def _ranged(low: float | None, high: float | None, *, name: str) -> None:
    if low is not None and high is not None and low > high:
        raise ValueError(f"min_{name} ({low}) is greater than max_{name} ({high})")
    for label, value in (("min", low), ("max", high)):
        if value is not None and value < 0:
            raise ValueError(f"{label}_{name} cannot be negative, got {value}")


def _round(value: Any, digits: int = 0) -> Any:
    if not isinstance(value, int | float):
        return None
    return round(value, digits) if digits else round(value)


def _measures(row: dict[str, Any]) -> dict[str, Any]:
    """The device's own summary figures, with their units in the key names."""
    moving, elapsed = row.get("total_timer_time_s"), row.get("total_elapsed_time_s")
    measures: dict[str, Any] = {
        "distance_km": _round(
            row["total_distance_m"] / 1000 if row.get("total_distance_m") is not None else None,
            2,
        ),
        "ascent_m": _round(row.get("total_ascent_m")),
        "descent_m": _round(row.get("total_descent_m")),
        "moving_time_s": _round(moving),
        "moving_time": format_duration(moving),
        "elapsed_time_s": _round(elapsed),
        "elapsed_time": format_duration(elapsed),
    }
    return {k: v for k, v in measures.items() if v is not None}


def _measures_source(row: dict[str, Any]) -> str:
    if row.get("sport") == "multisport":
        return "sum of the legs' FIT session summaries"
    return "FIT session summary"


def find_local_activities(
    latitude: float,
    longitude: float,
    radius_km: float = 10.0,
    activity_type: str | None = "running",
    sub_sport: str | None = None,
    min_distance_km: float | None = None,
    max_distance_km: float | None = None,
    min_ascent_m: float | None = None,
    max_ascent_m: float | None = None,
    min_duration_min: float | None = None,
    max_duration_min: float | None = None,
    duration_basis: str = "moving",
    sort: str = "nearest",
    limit: int = DEFAULT_FIND_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Find recorded runs that started near a point — candidates for a training route.

    Args:
        latitude: Decimal degrees, WGS84 (e.g. 49.2583 for Reims).
        longitude: Decimal degrees, WGS84 (e.g. 4.0317).
        radius_km: Search radius around that point (default 10, max 300).
        activity_type: Sport to search: "running" (default; includes trail
            running, which is a sub-sport), "cycling", "hiking"... None for all.
        sub_sport: Narrow further, e.g. "trail". Optional.
        min_distance_km, max_distance_km: Distance range.
        min_ascent_m, max_ascent_m: Positive elevation gain (D+) range.
        min_duration_min, max_duration_min: Duration range, in minutes, on the
            time chosen by `duration_basis`.
        duration_basis: "moving" (default: the stopwatch time, paused periods
            excluded) or "elapsed" (start to finish, pauses included). The two
            differ on any run with stops, and are returned separately.
        sort: "nearest" (default) or "recent".
        limit: Results per page (default 20, max 100).
        offset: Results to skip, for the next page.

    **What the radius means.** It is a great-circle (straight-line) distance
    from the search point to the activity's recorded *start point* — not to any
    other point of the track, and not a road distance or a travel time. A run
    that begins 30 km away and passes through the circle is not returned.

    A range on a measure the activity does not have (no ascent recorded)
    excludes it: a missing value cannot satisfy "at least 800 m of climb".

    Measures are the figures the device wrote in the FIT session summary,
    reported as recorded; nothing is recomputed from the GPS samples. Each
    result carries quality indicators: `has_gps`, `has_altitude`, and
    `partial_track` with the reasons (gaps in the recording, fixes missing,
    first fix late, no fix at the end).
    """
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"latitude must be between -90 and 90, got {latitude}")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"longitude must be between -180 and 180, got {longitude}")
    if not 0 < radius_km <= MAX_RADIUS_KM:
        raise ValueError(
            f"radius_km must be above 0 and at most {MAX_RADIUS_KM:g}, got {radius_km}"
        )
    _ranged(min_distance_km, max_distance_km, name="distance_km")
    _ranged(min_ascent_m, max_ascent_m, name="ascent_m")
    _ranged(min_duration_min, max_duration_min, name="duration_min")

    limit = max(1, min(int(limit), MAX_FIND_LIMIT))
    offset = max(0, int(offset))

    def _km(value: float | None) -> float | None:
        return value * 1000 if value is not None else None

    def _seconds(value: float | None) -> float | None:
        return value * 60 if value is not None else None

    with reading(_settings()) as conn:
        rows, total = queries.find_activities_near(
            conn,
            lat=latitude,
            lon=longitude,
            radius_m=radius_km * 1000,
            sport=activity_type,
            sub_sport=sub_sport,
            min_distance_m=_km(min_distance_km),
            max_distance_m=_km(max_distance_km),
            min_ascent_m=min_ascent_m,
            max_ascent_m=max_ascent_m,
            min_duration_s=_seconds(min_duration_min),
            max_duration_s=_seconds(max_duration_min),
            duration_basis=duration_basis,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        quality = queries.gps_quality(conn, [r["activity_id"] for r in rows], GAP_THRESHOLD_S)

    activities = []
    for row in rows:
        entry: dict[str, Any] = {
            "activity_id": row["activity_id"],
            "name": row.get("name"),
            "sport": row.get("sport"),
            "sub_sport": row.get("sub_sport"),
            "start_local": row["start_time_local"].strftime("%Y-%m-%dT%H:%M"),
            "start": {"lat": round(row["start_lat"], 5), "lon": round(row["start_lon"], 5)},
            "start_point_distance_km": round(row["start_distance_m"] / 1000, 2),
            "measures": _measures(row),
            "quality": routes.assess_quality(quality.get(row["activity_id"]), GAP_THRESHOLD_S),
        }
        if row.get("sport") == "multisport":
            entry["measures_source"] = _measures_source(row)
        if row.get("parent_activity_id") is not None:
            entry["leg_of_activity_id"] = row["parent_activity_id"]
        activities.append({k: v for k, v in entry.items() if v is not None})

    returned_end = offset + len(activities)
    log.info(
        "tool.find_local_activities",
        returned=len(activities),
        total=total,
        radius_km=radius_km,
        sport=activity_type,
    )
    result: dict[str, Any] = {
        "query": {
            "radius_km": radius_km,
            "radius_means": "straight-line distance from the search point to each "
            "activity's START point; not the whole track, not road or travel time",
            "activity_type": activity_type,
            "sub_sport": sub_sport,
            "duration_basis": duration_basis,
            "sort": sort,
        },
        "total_matches": total,
        "returned": len(activities),
        "offset": offset,
        "has_more": returned_end < total,
        "activities": activities,
        "measures_source": "FIT session summary, as written by the device; not recomputed",
        "gap_threshold_s": GAP_THRESHOLD_S,
    }
    if returned_end < total:
        result["next_offset"] = returned_end
    if not activities and total == 0:
        result["note"] = (
            "nothing started inside this circle with these filters. Widen radius_km, relax "
            "the ranges, or call database_status to check how far back the history goes."
        )
    return result


def get_activity_route(activity_id: int, max_points: int = DEFAULT_ROUTE_POINTS) -> dict[str, Any]:
    """The GPS track of one recorded activity: a bounded version for a map, plus the GPX.

    Args:
        activity_id: The activity's id, as returned by find_local_activities
            or list_activities.
        max_points: Upper bound on points in the returned track (default 500,
            max 2000). The track is simplified to fit, and the response says
            by how much.

    The geometry comes from the per-sample GPS data kept at ingestion — one
    point per recorded sample — never from the already-averaged streams
    returned by get_activity_streams. `track.simplification` states how many
    points existed, how many are returned, and the largest distance any
    dropped point lies from the returned line.

    **Interruptions are not bridged.** Where the recording has a hole longer
    than `gap_threshold_s` (a pause, a lost signal) the track is split into
    separate segments and the hole is listed under `interruptions`. A
    consumer drawing it must not join the segments.

    Nothing is invented: a fix without altitude has no altitude, a missing
    cumulative distance is null, and `missing_data` says what is absent. The
    full-resolution track — every valid fix — is the GPX behind
    `gpx.resource_uri`, read with the MCP `resources/read` request. It is
    generated on demand from the stored samples, so it has no expiry and no
    file on disk; it is available whenever this activity is in the database and
    the server is reachable. Do not assume any local path.

    Summary figures under `summary` are the device's, as recorded, and are
    never replaced by values recomputed from the track.
    """
    activity_id = int(activity_id)
    max_points = max(2, min(int(max_points), MAX_ROUTE_POINTS))

    with reading(_settings()) as conn:
        route = routes.load_activity_route(conn, activity_id)
        stats = queries.gps_quality(conn, [activity_id], GAP_THRESHOLD_S).get(activity_id)

    activity = route.activity
    result: dict[str, Any] = {
        "activity_id": activity_id,
        "name": activity.get("name"),
        "sport": activity.get("sport"),
        "sub_sport": activity.get("sub_sport"),
        "start_local": activity["start_time_local"].strftime("%Y-%m-%dT%H:%M"),
        "summary": _measures(activity) | {"source": _measures_source(activity)},
        "quality": routes.assess_quality(stats, route.gap_threshold_s),
        "gap_threshold_s": route.gap_threshold_s,
        "notes": [_TRACK_DISCLAIMER],
    }

    if route.fix_count == 0:
        result["track"] = None
        result["gpx"] = None
        result["notes"].append(
            "no valid GPS fix is stored for this activity, so there is no track to draw or export"
        )
        return result

    simplified = simplify_segments(route.segments, max_points)
    result["track"] = {
        "simplification": {
            "method": "ranked Douglas-Peucker (planar, per segment; segment ends always kept)",
            "points_available": route.fix_count,
            "points_returned": simplified.points_kept,
            "simplified": simplified.points_kept < route.fix_count,
            "max_deviation_m": round(simplified.max_deviation_m, 1),
        },
        "bounds": _bounds(route),
        "segments": [_segment_columns(segment) for segment in simplified.segments],
    }
    result["interruptions"] = [
        {
            "after_elapsed_s": round(i.after_elapsed_s),
            "gap_s": round(i.gap_s),
            "device_distance_during_gap_m": _round(i.gap_distance_m),
        }
        for i in route.interruptions
    ]
    result["missing_data"] = _missing_data(route)

    gpx_text = routes.activity_gpx(route)
    payload = gpx_text.encode("utf-8")
    result["gpx"] = {
        "resource_uri": ACTIVITY_GPX_URI.format(activity_id=activity_id),
        "mime_type": GPX_MIME_TYPE,
        "points": route.fix_count,
        "segments": len(route.segments),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "content": "every valid GPS fix, unsimplified, one <trkseg> per uninterrupted "
        "segment; <ele> only where altitude was recorded",
        "availability": "generated on demand from the stored samples; no expiry, no file "
        "written; read it with MCP resources/read",
    }

    log.info(
        "tool.get_activity_route",
        activity_id=activity_id,
        fixes=route.fix_count,
        returned=simplified.points_kept,
    )
    return result


def _bounds(route: routes.ActivityRoute) -> dict[str, float]:
    points = [p for s in route.segments for p in s]
    lats, lons = [p.lat for p in points], [p.lon for p in points]
    return {
        "min_lat": round(min(lats), 5),
        "max_lat": round(max(lats), 5),
        "min_lon": round(min(lons), 5),
        "max_lon": round(max(lons), 5),
    }


def _segment_columns(segment: list[Any]) -> dict[str, Any]:
    """One segment, column by column — about a third of the tokens of a list of objects."""
    columns: dict[str, Any] = {
        "lat": [round(p.lat, 5) for p in segment],
        "lon": [round(p.lon, 5) for p in segment],
        "elapsed_s": [round(p.elapsed_s) for p in segment],
    }
    if any(p.altitude_m is not None for p in segment):
        columns["altitude_m"] = [_round(p.altitude_m, 1) for p in segment]
    if any(p.distance_m is not None for p in segment):
        columns["distance_km"] = [
            round(p.distance_m / 1000, 3) if p.distance_m is not None else None for p in segment
        ]
    return columns


def _missing_data(route: routes.ActivityRoute) -> list[str]:
    missing = []
    fixes = route.fix_count
    if route.altitude_count < fixes:
        missing.append(f"altitude absent on {fixes - route.altitude_count} of {fixes} fixes")
    if route.distance_count < fixes:
        missing.append(
            f"cumulative distance absent on {fixes - route.distance_count} of {fixes} fixes"
        )
    left_out = route.samples_total - fixes
    if left_out > 0:
        missing.append(
            f"{left_out} of {route.samples_total} samples have no valid GPS fix and are left "
            "out, not interpolated"
        )
    if route.interruptions:
        missing.append(f"{len(route.interruptions)} recording gap(s): segments are not joined")
    return missing


def _parse_id(value: str, *, kind: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{kind} id must be an integer, got {value!r}") from exc


def activity_route_gpx(activity_id: str) -> str:
    """The full-resolution track of a recorded activity, as GPX 1.1."""
    with reading(_settings()) as conn:
        route = routes.load_activity_route(conn, _parse_id(activity_id, kind="activity"))
    if route.fix_count == 0:
        raise ValueError(f"activity {activity_id} has no valid GPS fix to export")
    return routes.activity_gpx(route)


def course_route_gpx(course_id: str) -> str:
    """A saved Garmin course's stored geometry, as GPX 1.1."""
    with reading(_settings()) as conn:
        return routes.course_gpx(conn, _parse_id(course_id, kind="course"))


__all__ = [
    "ACTIVITY_GPX_URI",
    "COURSE_GPX_URI",
    "activity_route_gpx",
    "course_route_gpx",
    "find_local_activities",
    "get_activity_route",
]

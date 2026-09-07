"""Mapping Garmin's course JSON onto `Course`.

Two calls, two shapes, one row. The listing (`course_reader.list_courses`)
gives the summary fields cheaply for every course; the per-course detail
(`course_reader.get_course`) is the only place the geometry — `geoPoints`,
the track, and `coursePoints`, the named waypoints — actually lives. Reading
defensively throughout, the same rule as `sleep_parser`: this is an
undocumented endpoint, and nothing here comes with a guarantee it stays
shaped this way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from garmin_mcp.domain.models import Course, CourseTrackPoint, CourseWaypoint

# Present on both the listing entry and the detail response, but under
# different keys — the detail's own `activityTypePk` is a bare numeric id
# with no name attached, so the sport string only ever comes from the list.
_DETAIL_ONLY_KEYS = frozenset({"geoPoints", "coursePoints"})


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _epoch_ms(value: Any) -> datetime | None:
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def parse_course_summary(raw: dict[str, Any]) -> Course:
    """One entry from the course listing — no geometry yet."""
    activity_type = raw.get("activityType") or {}
    return Course(
        course_id=int(raw["courseId"]),
        name=raw.get("courseName"),
        sport=activity_type.get("typeKey"),
        distance_m=_float(raw.get("distanceInMeters")),
        elevation_gain_m=_float(raw.get("elevationGainInMeters")),
        elevation_loss_m=_float(raw.get("elevationLossInMeters")),
        start_lat=_float(raw.get("startLatitude")),
        start_lon=_float(raw.get("startLongitude")),
        created_at=_epoch_ms(raw.get("createdDate")),
        updated_at=_epoch_ms(raw.get("updatedDate")),
        favorite=bool(raw.get("favorite")),
        extra=raw,
    )


def attach_detail(course: Course, detail: dict[str, Any]) -> Course:
    """Fill in a summary-only `Course` with its track and waypoints.

    Mutates and returns `course` — called once, right after
    `parse_course_summary`, so there is nothing else holding a reference to
    fight over.
    """
    course.track = [
        CourseTrackPoint(
            point_index=index,
            lat=_float(point.get("latitude")),
            lon=_float(point.get("longitude")),
            elevation_m=_float(point.get("elevation")),
            distance_m=_float(point.get("distance")),
        )
        for index, point in enumerate(detail.get("geoPoints") or [])
    ]
    course.waypoints = [
        CourseWaypoint(
            waypoint_index=index,
            name=point.get("name"),
            point_type=point.get("coursePointType"),
            distance_m=_float(point.get("distance")),
            lat=_float(point.get("lat")),
            lon=_float(point.get("lon")),
            # `derivedElevation` is Garmin's own reading of the track at this
            # distance; `elevation` on a hand-placed waypoint is routinely 0.
            elevation_m=_float(point.get("derivedElevation")) or _float(point.get("elevation")),
        )
        for index, point in enumerate(detail.get("coursePoints") or [])
    ]
    # The track and waypoints are normalised into their own tables; keeping
    # them here too would just double the storage for no benefit. Everything
    # else from the detail response — bounding box, notes, flags — has no
    # dedicated column, so it still belongs in `extra`.
    course.extra = {
        **course.extra,
        **{k: v for k, v in detail.items() if k not in _DETAIL_ONLY_KEYS},
    }
    return course

"""Garmin's Courses library: saved GPX routes, not activities.

A "course" in Garmin's own vocabulary is a route imported or drawn ahead of
time — a plan to run, never itself a recorded session. It lives behind a
completely different part of Garmin Connect (Routes/Courses) than everything
else this project reads, and neither `garminconnect` nor any other Python
client wraps it: as of this writing there is no `get_courses` anywhere in the
ecosystem.

The endpoint itself — `course-service` — is not undiscoverable, just
undocumented for Python. Browser network traces from unrelated tools (GPX
export scripts, route-import utilities) that already reverse-engineered it
show the same base path Garmin's own web client calls:
`https://connect.garmin.com/modern/proxy/course-service/course/...`. The
`/modern/proxy` prefix is what the browser's own SPA backend adds when
relaying a same-origin fetch; `garminconnect`'s `connectapi()` — already
proven working here for sleep (`health_reader.py`) — talks to the same
service directly without that prefix, and that was confirmed live against a
real account rather than assumed: `GET /course-service/course` returns the
athlete's saved courses, and `GET /course-service/course/{id}` returns one
in full, geometry included.

Kept apart from `ActivitySource` for the same reason `health_reader` is: a
narrow, activity-only contract should not grow an endpoint for an unrelated
feature just because both happen to live on connect.garmin.com.
"""

from __future__ import annotations

from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.errors import GarminAuthError
from garmin_mcp.logging import get_logger

log = get_logger(__name__)


def _client(settings: Settings) -> Any:
    """Resume the saved session. Never a fresh login — this runs unattended."""
    import garminconnect

    token_dir = settings.token_dir
    if not token_dir.is_dir() or not any(token_dir.iterdir()):
        raise GarminAuthError(f"no saved Garmin session in {token_dir}")

    client = garminconnect.Garmin()
    try:
        client.login(str(token_dir))
    except Exception as exc:
        from garmin_mcp.garmin.cffi_source import _translate

        raise _translate(exc) from exc
    return client


def list_courses(settings: Settings | None = None) -> list[dict[str, Any]]:
    """The athlete's saved courses, as Garmin's summary listing returns them.

    Cheap — one request, no geometry — which is what lets the sync refresh
    this every cycle and only fetch a course's full detail once, the first
    time it is seen.
    """
    settings = settings or get_settings()
    client = _client(settings)
    try:
        courses = client.connectapi("/course-service/course")
    except Exception as exc:
        from garmin_mcp.garmin.cffi_source import _translate

        raise _translate(exc) from exc
    return courses or []


def get_course(course_id: int, settings: Settings | None = None) -> dict[str, Any] | None:
    """One course in full: geometry (`geoPoints`) and named waypoints
    (`coursePoints`) included. The expensive call — hundreds to low
    thousands of points — so the sync makes it once per course, not per
    cycle.
    """
    settings = settings or get_settings()
    client = _client(settings)
    try:
        detail: dict[str, Any] | None = client.connectapi(f"/course-service/course/{course_id}")
    except Exception as exc:
        from garmin_mcp.garmin.cffi_source import _translate

        raise _translate(exc) from exc
    return detail

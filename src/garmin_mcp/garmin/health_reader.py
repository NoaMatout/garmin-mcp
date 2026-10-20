"""Health metrics Garmin's watch computes but `ActivitySource` never touches.

Kept apart from `ActivitySource` for the same reason `workout_writer` is:
that interface is a narrow, deliberately activity-only contract shared by two
very different backends (a lightweight HTTP client and a headless browser),
and folding sleep into it would force the browser backend to grow an endpoint
nobody has verified against a live account. This module talks to the
credentialed `garminconnect` client directly, resuming the saved session the
same way `workout_writer._client` does.

Read-only, so unlike `workout_writer` it needs no `GARMIN_ENABLE_WRITES`
gate — reading someone's sleep carries the same risk profile as reading their
activities, not the risk profile of writing to their account.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.errors import GarminAuthError, GarminRateLimitError
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


def fetch_sleep(
    days: list[date],
    settings: Settings | None = None,
) -> dict[date, dict[str, Any] | None]:
    """Garmin's raw daily-sleep payload for each date, over one resumed session.

    A `None` value means this specific date failed after the session itself
    was established — recorded rather than raised, so one bad night does not
    abort the rest of the window. A rate limit is the exception: it stops the
    whole window immediately rather than hammering a service already saying
    no, the same rule `pipeline.sync_from_garmin` follows for activities.

    Raises `GarminAuthError` (and any other session-level `GarminError`)
    before fetching anything, if the session itself cannot be resumed.
    """
    settings = settings or get_settings()
    client = _client(settings)

    results: dict[date, dict[str, Any] | None] = {}
    for day in days:
        try:
            results[day] = client.get_sleep_data(day.isoformat())
        except Exception as exc:
            from garmin_mcp.garmin.cffi_source import _translate

            translated = _translate(exc)
            if isinstance(translated, GarminRateLimitError):
                log.warning("sleep.rate_limited_stopping", completed=len(results))
                raise translated from exc
            log.warning("sleep.fetch_failed", date=str(day), error=str(translated))
            results[day] = None

    return results

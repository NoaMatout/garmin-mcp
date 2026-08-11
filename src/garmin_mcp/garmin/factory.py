"""Choosing a backend.

`GARMIN_BACKEND=auto` is the interesting case. It tries the cheap client first
and falls back to the browser only when the cheap one reports that it cannot
work — and only for reasons a browser would actually fix.

That distinction matters. If the token has merely expired, launching Chromium
changes nothing: a human still has to log in. Falling back there would swap a
clear "run `garmin-mcp auth`" for a slow, confusing browser failure. The
fallback is therefore reserved for the case it was built for — Garmin refusing
the client outright on the strength of its TLS handshake.
"""

from __future__ import annotations

from garmin_mcp.config import Backend, Settings, get_settings
from garmin_mcp.errors import GarminAuthError, GarminError
from garmin_mcp.garmin.source import ActivitySource, SourceHealth, SourceStatus
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

# Conditions a real browser can plausibly resolve. NEEDS_AUTH is deliberately
# absent: no backend can invent a login the user has not performed.
_BROWSER_FIXABLE = frozenset({SourceHealth.BLOCKED, SourceHealth.UNAVAILABLE})


def build_source(
    backend: Backend | None = None,
    settings: Settings | None = None,
) -> ActivitySource:
    """Return a backend without contacting Garmin.

    Construction is deliberately free of network access so the worker can be
    created, inspected and unit-tested offline.
    """
    settings = settings or get_settings()
    backend = backend or settings.backend

    if backend is Backend.PLAYWRIGHT:
        from garmin_mcp.garmin.playwright_source import PlaywrightSource

        return PlaywrightSource(settings)

    from garmin_mcp.garmin.cffi_source import CffiSource

    return CffiSource(settings)


def resolve_source(
    settings: Settings | None = None,
    *,
    backend: Backend | None = None,
) -> tuple[ActivitySource, SourceStatus]:
    """Return a *working* backend, falling back when that is the right answer.

    Raises `GarminAuthError` when the only thing missing is a login, so the
    caller can say something useful instead of retrying forever.
    """
    settings = settings or get_settings()
    backend = backend or settings.backend

    if backend is not Backend.AUTO:
        source = build_source(backend, settings)
        status = source.health_check()
        if not status.usable:
            _raise_for(status)
        return source, status

    primary = build_source(Backend.CFFI, settings)
    status = primary.health_check()
    if status.usable:
        return primary, status

    if status.health not in _BROWSER_FIXABLE:
        primary.close()
        _raise_for(status)

    log.warning(
        "garmin.falling_back_to_browser",
        reason=status.health.value,
        detail=status.detail,
    )
    primary.close()

    from garmin_mcp.garmin.playwright_source import PlaywrightSource

    fallback = PlaywrightSource(settings)
    fallback_status = fallback.health_check()
    if not fallback_status.usable:
        fallback.close()
        raise GarminError(
            f"no usable Garmin backend — {status.describe()}; {fallback_status.describe()}",
            hint="drop FIT files into data/inbox/ and run `garmin-mcp import`; "
            "manual import needs neither credentials nor network",
        )
    return fallback, fallback_status


def _raise_for(status: SourceStatus) -> None:
    if status.health is SourceHealth.NEEDS_AUTH:
        raise GarminAuthError(status.describe())
    raise GarminError(
        status.describe(),
        hint="drop FIT files into data/inbox/ and run `garmin-mcp import` to "
        "keep ingesting without Garmin",
    )

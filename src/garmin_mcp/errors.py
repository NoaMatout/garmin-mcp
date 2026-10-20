"""Typed errors.

Every failure mode that a user can plausibly hit gets its own class with a
`hint` — the actionable half of the message. Garmin auth is the fragile part
of this system (see README), so those errors carry the most context.
"""

from __future__ import annotations

from typing import ClassVar


class GarminMcpError(Exception):
    """Base class for everything this project raises on purpose."""

    # A stable, machine-readable identifier. Messages are written for people and
    # change; a client deciding what to do next (re-authenticate, start the
    # worker, retry) branches on this instead.
    code: ClassVar[str] = "error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} — {self.hint}" if self.hint else self.message

    def as_dict(self) -> dict[str, str | None]:
        """The error as data: what happened, in a form to branch on, and what to do."""
        return {"code": self.code, "message": self.message, "recovery": self.hint}


# ─── Garmin ───────────────────────────────────────────────────────────


class GarminError(GarminMcpError):
    """Anything going wrong on the Garmin side."""

    code = "garmin_unavailable"


class GarminAuthError(GarminError):
    """Credentials rejected, token expired, or MFA required.

    Recoverable by the user, never by the server: re-run `garmin-mcp auth`.
    """

    code = "garmin_auth_required"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint=hint or "run `garmin-mcp auth` to re-authenticate interactively",
        )


class GarminBlockedError(GarminError):
    """Cloudflare refused the client before authentication even started.

    Garmin fingerprints the TLS handshake, so a plain HTTP client is rejected
    on sight (HTTP 403/429). This is the failure that killed `garth`, and the
    reason the playwright backend exists.
    """

    code = "garmin_blocked"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint=hint
            or "Garmin is blocking non-browser clients; try GARMIN_BACKEND=playwright, "
            "or fall back to manual import by dropping FIT files in data/inbox/",
        )


class GarminRateLimitError(GarminError):
    """Too many requests — back off rather than retry immediately."""

    code = "garmin_rate_limited"


# ─── Ingestion ────────────────────────────────────────────────────────


class FitParseError(GarminMcpError):
    """A FIT file could not be decoded, or lacks the messages we require."""

    code = "fit_parse_failed"

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class IngestError(GarminMcpError):
    """Storage or database write failed during ingestion."""

    code = "ingest_failed"


# ─── Server ───────────────────────────────────────────────────────────


class ActivityNotFoundError(GarminMcpError):
    """Requested activity is not in the database."""

    code = "activity_not_found"

    def __init__(self, activity_id: int) -> None:
        super().__init__(
            f"activity {activity_id} not found",
            hint="run `garmin-mcp sync` to pull recent activities, "
            "or check the id with list_activities",
        )
        self.activity_id = activity_id


class RouteUnavailableError(GarminMcpError):
    """A route was asked for that the stored data cannot provide."""

    code = "route_unavailable"


class WorkerUnavailableError(GarminMcpError):
    """sync_now was called but no ingest worker picked up the trigger."""

    code = "worker_unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            hint="start the ingest worker with `docker compose up -d ingest` "
            "or `garmin-mcp worker`",
        )


class DatabaseLockedError(GarminMcpError):
    """The DuckDB file is held by the writer and did not free up in time."""

    code = "database_locked"

    def __init__(self, path: str) -> None:
        super().__init__(
            f"database {path} is locked by another process",
            hint="an ingest run is in progress; retry in a few seconds",
        )

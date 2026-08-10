"""Typed errors.

Every failure mode that a user can plausibly hit gets its own class with a
`hint` — the actionable half of the message. Garmin auth is the fragile part
of this system (see README), so those errors carry the most context.
"""

from __future__ import annotations


class GarminMcpError(Exception):
    """Base class for everything this project raises on purpose."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} — {self.hint}" if self.hint else self.message


# ─── Garmin ───────────────────────────────────────────────────────────


class GarminError(GarminMcpError):
    """Anything going wrong on the Garmin side."""


class GarminAuthError(GarminError):
    """Credentials rejected, token expired, or MFA required.

    Recoverable by the user, never by the server: re-run `garmin-mcp auth`.
    """

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

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint=hint
            or "Garmin is blocking non-browser clients; try GARMIN_BACKEND=playwright, "
            "or fall back to manual import by dropping FIT files in data/inbox/",
        )


class GarminRateLimitError(GarminError):
    """Too many requests — back off rather than retry immediately."""


# ─── Ingestion ────────────────────────────────────────────────────────


class FitParseError(GarminMcpError):
    """A FIT file could not be decoded, or lacks the messages we require."""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class IngestError(GarminMcpError):
    """Storage or database write failed during ingestion."""


# ─── Server ───────────────────────────────────────────────────────────


class ActivityNotFoundError(GarminMcpError):
    """Requested activity is not in the database."""

    def __init__(self, activity_id: int) -> None:
        super().__init__(
            f"activity {activity_id} not found",
            hint="run `garmin-mcp sync` to pull recent activities, "
            "or check the id with list_activities",
        )
        self.activity_id = activity_id


class WorkerUnavailableError(GarminMcpError):
    """sync_now was called but no ingest worker picked up the trigger."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            hint="start the ingest worker with `docker compose up -d ingest` "
            "or `garmin-mcp worker`",
        )


class DatabaseLockedError(GarminMcpError):
    """The DuckDB file is held by the writer and did not free up in time."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"database {path} is locked by another process",
            hint="an ingest run is in progress; retry in a few seconds",
        )

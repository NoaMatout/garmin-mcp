"""The contract every Garmin backend implements.

This interface is the single point of contact with Garmin, and it exists
because that contact is the fragile part of the system. In March 2026 Garmin
deployed Cloudflare TLS fingerprinting, `garth` was deprecated within days, and
every plain HTTP client stopped working. Something similar will happen again.

Keeping the surface this narrow — list what exists, fetch one file, say whether
you are healthy — means the next break is confined to one module. Two
implementations already sit behind it (a lightweight HTTP client that
impersonates a browser's TLS handshake, and a real headless browser), and an
official-API backend can be added the day Garmin reopens its developer
programme, without the ingestion pipeline noticing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable

from garmin_mcp.domain.models import ActivityStub


class SourceHealth(StrEnum):
    OK = "ok"
    NEEDS_AUTH = "needs_auth"       # no token, or it expired — a human must log in
    BLOCKED = "blocked"             # Cloudflare refused the client outright
    RATE_LIMITED = "rate_limited"   # back off and retry later
    UNAVAILABLE = "unavailable"     # network down, Garmin down, backend not installed


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """A backend's self-assessment, safe to show a user.

    Never contains credentials or tokens — this ends up in logs and in MCP tool
    output.
    """

    health: SourceHealth
    backend: str
    detail: str | None = None
    profile_name: str | None = None

    @property
    def usable(self) -> bool:
        return self.health is SourceHealth.OK

    def describe(self) -> str:
        parts = [f"{self.backend}: {self.health.value}"]
        if self.profile_name:
            parts.append(f"as {self.profile_name}")
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


@runtime_checkable
class ActivitySource(Protocol):
    """A read-only view of what Garmin holds.

    Implementations must be non-interactive: a backend is used by the ingest
    worker and must never block waiting for a password or an MFA code. When
    credentials are missing or stale, raise `GarminAuthError` and let the CLI
    handle the human part.
    """

    name: str

    def health_check(self) -> SourceStatus:
        """Report whether this backend can currently talk to Garmin.

        Must not raise: an unreachable backend is a status, not an exception,
        because the factory uses this to choose between backends.
        """
        ...

    def list_activities(
        self,
        since: date | None = None,
        limit: int = 25,
        activity_type: str | None = None,
    ) -> list[ActivityStub]:
        """Summaries of recent activities, newest first.

        Cheap enough to call on every sync: it decides what still needs
        downloading without fetching any FIT files.
        """
        ...

    def download_fit(self, activity_id: int) -> bytes:
        """Fetch one activity's original file.

        Returns whatever Garmin sends — which is a ZIP containing the FIT, not
        the FIT itself. Unwrapping belongs to the storage layer, so every
        backend can stay faithful to what it received.
        """
        ...

    def close(self) -> None:
        """Release any browser or session held by this backend."""
        ...

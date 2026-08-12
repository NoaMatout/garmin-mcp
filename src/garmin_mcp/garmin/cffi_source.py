"""Default backend: `garminconnect` over curl_cffi.

Garmin fingerprints the TLS handshake, so a stock `requests` client is refused
before authentication even begins. `curl_cffi` reproduces Chrome's handshake
byte for byte, which gets past it — for now. This is an arms race, and the
honest description of this backend is "works today, may stop without warning".
That is precisely why `ActivitySource` exists and why the manual inbox is a
first-class ingestion path.

What this module never does is authenticate interactively. It resumes a token
saved by `garmin-mcp auth` and fails loudly when there is none, because the
ingest worker runs unattended and a backend that blocks on an MFA prompt would
hang it forever.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.domain.models import ActivityStub
from garmin_mcp.errors import (
    GarminAuthError,
    GarminBlockedError,
    GarminError,
    GarminRateLimitError,
)
from garmin_mcp.garmin.source import SourceHealth, SourceStatus
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

BACKEND_NAME = "cffi"

# Garmin caps a single activity-list page.
_MAX_PAGE = 100


def _translate(exc: Exception) -> GarminError:
    """Turn a garminconnect exception into one of ours, with a way forward.

    The distinction that matters: "your token expired" is fixed by logging in
    again, while "Cloudflare refused this client" is not — no amount of
    re-authenticating helps, and the answer is a different backend or the
    manual inbox.
    """
    import garminconnect

    text = str(exc)
    if isinstance(exc, garminconnect.GarminConnectTooManyRequestsError):
        return GarminRateLimitError(f"Garmin is rate limiting this client: {text}")
    if isinstance(exc, garminconnect.GarminConnectAuthenticationError):
        if "required" in text.lower():
            return GarminAuthError("no saved Garmin session")
        return GarminAuthError(f"Garmin rejected the session: {text}")
    if "403" in text or "cloudflare" in text.lower() or "429" in text:
        return GarminBlockedError(f"Garmin refused the connection: {text}")
    return GarminError(f"Garmin request failed: {text}")


def _to_stub(raw: dict[str, Any]) -> ActivityStub | None:
    """Map one activity-list entry, tolerating Garmin's shifting field names."""
    activity_id = raw.get("activityId")
    started = raw.get("startTimeGMT") or raw.get("startTimeLocal")
    if activity_id is None or not started:
        return None

    try:
        # Garmin serves "2026-03-15 07:30:00" — space separated, no zone marker.
        start = datetime.fromisoformat(str(started).replace(" ", "T")).replace(tzinfo=UTC)
    except ValueError:
        log.warning("garmin.unparsable_start_time", activity_id=activity_id, value=started)
        return None

    type_key = (raw.get("activityType") or {}).get("typeKey")
    return ActivityStub(
        activity_id=int(activity_id),
        start_time_utc=start,
        sport=type_key,
        name=raw.get("activityName"),
        distance_m=raw.get("distance"),
        duration_s=raw.get("duration"),
    )


class CffiSource:
    """Talks to Garmin Connect through `garminconnect` + curl_cffi."""

    name = BACKEND_NAME

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any | None = None
        self._profile: str | None = None

    # ─── session ──────────────────────────────────────────────────────

    def _connect(self) -> Any:
        """Resume the saved session. Never prompts, never uses a password."""
        if self._client is not None:
            return self._client

        try:
            import garminconnect
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise GarminError("garminconnect is not installed; run `uv sync`") from exc

        token_dir = self._settings.token_dir
        if not token_dir.is_dir() or not any(token_dir.iterdir()):
            raise GarminAuthError(f"no saved Garmin session in {token_dir}")

        # No credentials passed on purpose: this constructor must be incapable
        # of starting a fresh login, so it can only ever resume.
        client = garminconnect.Garmin()
        try:
            client.login(str(token_dir))
        except Exception as exc:
            raise _translate(exc) from exc

        self._client = client
        self._profile = getattr(client, "display_name", None) or getattr(client, "full_name", None)
        log.info("garmin.session_resumed", backend=self.name, profile=self._profile)
        return client

    def close(self) -> None:
        self._client = None

    # ─── ActivitySource ───────────────────────────────────────────────

    def health_check(self) -> SourceStatus:
        try:
            self._connect()
        except GarminAuthError as exc:
            return SourceStatus(SourceHealth.NEEDS_AUTH, self.name, exc.message)
        except GarminBlockedError as exc:
            return SourceStatus(SourceHealth.BLOCKED, self.name, exc.message)
        except GarminRateLimitError as exc:
            return SourceStatus(SourceHealth.RATE_LIMITED, self.name, exc.message)
        except Exception as exc:
            return SourceStatus(SourceHealth.UNAVAILABLE, self.name, str(exc))
        return SourceStatus(SourceHealth.OK, self.name, profile_name=self._profile)

    def list_activities(
        self,
        since: date | None = None,
        limit: int = 25,
        activity_type: str | None = None,
    ) -> list[ActivityStub]:
        client = self._connect()
        limit = max(1, min(limit, _MAX_PAGE))

        try:
            if since is not None:
                raw = client.get_activities_by_date(
                    since.isoformat(),
                    date.today().isoformat(),
                    activitytype=activity_type,
                )
            else:
                raw = client.get_activities(0, limit, activitytype=activity_type)
        except Exception as exc:
            raise _translate(exc) from exc

        if isinstance(raw, dict):
            raw = raw.get("activityList", [])

        stubs = [stub for entry in raw if (stub := _to_stub(entry)) is not None]
        stubs.sort(key=lambda s: s.start_time_utc, reverse=True)
        log.info("garmin.listed", backend=self.name, count=len(stubs), since=str(since))
        return stubs[:limit]

    def download_fit(self, activity_id: int) -> bytes:
        client = self._connect()
        try:
            import garminconnect

            data = client.download_activity(
                str(activity_id),
                dl_fmt=garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL,
            )
        except Exception as exc:
            raise _translate(exc) from exc

        if not data:
            raise GarminError(f"Garmin returned an empty file for activity {activity_id}")
        log.debug("garmin.downloaded", activity_id=activity_id, bytes=len(data))
        return bytes(data)

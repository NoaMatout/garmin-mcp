"""The ingest worker: the only process that writes.

DuckDB grants exclusive access to a single writer, and readers cannot open the
file while it is held. That constraint, not a preference for daemons, is what
shapes this module: one process owns writing, the MCP server only ever reads,
and the two coordinate through the filesystem rather than by fighting over a
lock.

Communication is three kinds of small JSON file under `data/.triggers/`:

    worker.alive              heartbeat, rewritten every poll
    sync-<id>.request.json    "please sync now", written by the MCP server
    sync-<id>.result.json     what happened, written back by the worker

Files rather than a socket or a queue, deliberately. Under stdio the MCP server
is a short-lived subprocess started by a desktop client; it has no port to bind
and no lifetime to speak of. A directory both processes can see needs no
service discovery, survives either side restarting, and can be inspected with
`ls` when something goes wrong.

The loop is synchronous. Every dependency it touches — garminconnect, duckdb,
Playwright's sync API — is blocking, so an event loop would add a layer to
debug without buying concurrency that anything here could use.
"""

from __future__ import annotations

import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.db.migrations import init_database
from garmin_mcp.errors import GarminAuthError, GarminError, WorkerUnavailableError
from garmin_mcp.ingest.pipeline import IngestReport, import_inbox, sync_from_garmin
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

HEARTBEAT_FILENAME = "worker.alive"
REQUEST_SUFFIX = ".request.json"
RESULT_SUFFIX = ".result.json"

# How often the loop wakes to look for a trigger. Short enough that `sync_now`
# feels immediate, long enough to be invisible in a `top` listing.
POLL_SECONDS = 2.0

# A heartbeat older than this means nobody is listening. Generously above the
# poll interval so a slow sync — which blocks the loop — is not mistaken for a
# dead worker.
HEARTBEAT_STALE_SECONDS = 90.0

# Requests nobody collected in this long are abandoned. Without it, a trigger
# written while the worker was down would fire at an arbitrary later moment.
REQUEST_EXPIRY_SECONDS = 300.0


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via a temporary file and a rename.

    The reader on the other side polls for existence, so a partially written
    file would be read as though it were complete.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, default=str), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return None


# ─── heartbeat, readable by anyone ────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    alive: bool
    pid: int | None = None
    updated_at: datetime | None = None
    last_sync: str | None = None
    detail: str | None = None

    @property
    def age_seconds(self) -> float | None:
        if self.updated_at is None:
            return None
        return (datetime.now(UTC) - self.updated_at).total_seconds()


def read_worker_status(settings: Settings | None = None) -> WorkerStatus:
    """Is a worker running, and how recently did it check in?

    Used by `sync_now` to fail fast with a useful message instead of waiting
    out a timeout for a process that was never started.
    """
    settings = settings or get_settings()
    payload = _read_json(settings.trigger_dir / HEARTBEAT_FILENAME)
    if payload is None:
        return WorkerStatus(alive=False, detail="no worker heartbeat found")

    try:
        updated = datetime.fromisoformat(payload["updated_at"])
    except (KeyError, ValueError):
        return WorkerStatus(alive=False, detail="unreadable heartbeat")

    age = (datetime.now(UTC) - updated).total_seconds()
    return WorkerStatus(
        alive=age <= HEARTBEAT_STALE_SECONDS,
        pid=payload.get("pid"),
        updated_at=updated,
        last_sync=payload.get("last_sync"),
        detail=None if age <= HEARTBEAT_STALE_SECONDS else f"heartbeat is {age:.0f}s old",
    )


# ─── requesting a sync from another process ───────────────────────────


def request_sync(
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Ask the worker to sync, and wait for its answer.

    Raises `WorkerUnavailableError` when no worker is listening, rather than
    silently leaving a request file for one that may never start.
    """
    settings = settings or get_settings()
    settings.trigger_dir.mkdir(parents=True, exist_ok=True)

    status = read_worker_status(settings)
    if not status.alive:
        raise WorkerUnavailableError(
            f"no ingest worker is running ({status.detail})"
        )

    request_id = uuid.uuid4().hex[:12]
    request_path = settings.trigger_dir / f"sync-{request_id}{REQUEST_SUFFIX}"
    result_path = settings.trigger_dir / f"sync-{request_id}{RESULT_SUFFIX}"

    _write_atomic(
        request_path,
        {"requested_at": datetime.now(UTC).isoformat(), "limit": limit},
    )
    log.info("sync_now.requested", request_id=request_id, limit=limit)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if result_path.exists():
            result = _read_json(result_path) or {}
            result_path.unlink(missing_ok=True)
            log.info("sync_now.completed", request_id=request_id)
            return result
        time.sleep(0.25)

    request_path.unlink(missing_ok=True)
    raise WorkerUnavailableError(
        f"the worker did not answer within {timeout_s:.0f}s — it may be busy "
        "with a long sync"
    )


# ─── the worker itself ────────────────────────────────────────────────


@dataclass
class IngestWorker:
    """Periodic sync, plus on-demand runs requested through the trigger files."""

    settings: Settings = field(default_factory=get_settings)
    _stopping: bool = field(default=False, init=False)
    _last_sync: datetime | None = field(default=None, init=False)

    # ─── lifecycle ────────────────────────────────────────────────────

    def install_signal_handlers(self) -> None:
        """Stop cleanly on SIGTERM and SIGINT.

        `docker stop` sends SIGTERM; without this the container is killed
        mid-write nine seconds later, which is exactly when a database is
        worth not corrupting.
        """
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self._request_stop)

    def _request_stop(self, signum: int, _frame: FrameType | None) -> None:
        log.info("worker.stopping", signal=signal.Signals(signum).name)
        self._stopping = True

    def run_forever(self) -> None:
        """Poll for work until asked to stop."""
        self.settings.ensure_dirs()
        init_database(self.settings)

        log.info(
            "worker.started",
            pid=os.getpid(),
            interval_minutes=self.settings.sync_interval_minutes,
            backend=self.settings.backend.value,
        )
        self._heartbeat()

        # Sync once at startup rather than waiting out a full interval: a
        # container that just came up should catch up immediately.
        self._scheduled_cycle()

        while not self._stopping:
            self._heartbeat()
            self._expire_stale_requests()
            self._serve_requests()

            if self._interval_elapsed():
                self._scheduled_cycle()

            time.sleep(POLL_SECONDS)

        self._clear_heartbeat()
        log.info("worker.stopped")

    # ─── work ─────────────────────────────────────────────────────────

    def _interval_elapsed(self) -> bool:
        if self._last_sync is None:
            return True
        elapsed = (datetime.now(UTC) - self._last_sync).total_seconds()
        return elapsed >= self.settings.sync_interval_minutes * 60

    def _scheduled_cycle(self) -> None:
        """One full pass: the inbox first, then Garmin."""
        self._last_sync = datetime.now(UTC)
        self._run_cycle(limit=None)

    def _run_cycle(self, *, limit: int | None) -> dict[str, Any]:
        """Import the inbox, then sync Garmin. Returns a report both can share.

        The inbox runs first and unconditionally: it needs no network and no
        credentials, so files waiting there must be ingested even when Garmin
        is unreachable — which is the entire point of that path existing.
        """
        outcome: dict[str, Any] = {"finished_at": datetime.now(UTC).isoformat()}

        try:
            inbox = import_inbox(self.settings)
            outcome["inbox"] = _report_dict(inbox)
        except Exception as exc:  # a bad file must not take the worker down
            log.exception("worker.inbox_failed")
            outcome["inbox_error"] = str(exc)

        try:
            outcome["garmin"] = _report_dict(self._sync_garmin(limit=limit))
        except GarminAuthError as exc:
            # Expected and recoverable by the user; not worth a stack trace.
            log.warning("worker.garmin_needs_auth", error=str(exc))
            outcome["garmin_error"] = str(exc)
            outcome["garmin_hint"] = "run `garmin-mcp auth`"
        except GarminError as exc:
            log.warning("worker.garmin_unavailable", error=str(exc))
            outcome["garmin_error"] = str(exc)
        except Exception as exc:
            log.exception("worker.sync_failed")
            outcome["garmin_error"] = str(exc)

        return outcome

    def _sync_garmin(self, *, limit: int | None) -> IngestReport:
        from garmin_mcp.garmin.factory import resolve_source

        source, status = resolve_source(self.settings)
        log.info("worker.syncing", backend=status.backend)
        try:
            return sync_from_garmin(source, self.settings, limit=limit)
        finally:
            source.close()

    # ─── trigger files ────────────────────────────────────────────────

    def _pending_requests(self) -> list[Path]:
        return sorted(self.settings.trigger_dir.glob(f"sync-*{REQUEST_SUFFIX}"))

    def _serve_requests(self) -> None:
        for request_path in self._pending_requests():
            payload = _read_json(request_path) or {}
            request_path.unlink(missing_ok=True)

            request_id = request_path.name[len("sync-") : -len(REQUEST_SUFFIX)]
            log.info("worker.serving_request", request_id=request_id)

            result = self._run_cycle(limit=payload.get("limit"))
            self._last_sync = datetime.now(UTC)

            _write_atomic(
                self.settings.trigger_dir / f"sync-{request_id}{RESULT_SUFFIX}",
                result,
            )
            self._heartbeat()

    def _expire_stale_requests(self) -> None:
        """Discard requests whose caller has long since given up."""
        cutoff = time.time() - REQUEST_EXPIRY_SECONDS
        for path in self._pending_requests():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    log.warning("worker.expired_request", file=path.name)
            except OSError:
                continue

        # Results nobody collected leak otherwise: the requester may have died.
        for path in self.settings.trigger_dir.glob(f"sync-*{RESULT_SUFFIX}"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    # ─── heartbeat ────────────────────────────────────────────────────

    def _heartbeat(self) -> None:
        _write_atomic(
            self.settings.trigger_dir / HEARTBEAT_FILENAME,
            {
                "pid": os.getpid(),
                "updated_at": datetime.now(UTC).isoformat(),
                "last_sync": self._last_sync.isoformat() if self._last_sync else None,
                "interval_minutes": self.settings.sync_interval_minutes,
            },
        )

    def _clear_heartbeat(self) -> None:
        """Remove the heartbeat on a clean exit.

        A stopped worker should report as stopped immediately, rather than
        making callers wait out the staleness window.
        """
        (self.settings.trigger_dir / HEARTBEAT_FILENAME).unlink(missing_ok=True)


def _report_dict(report: IngestReport) -> dict[str, Any]:
    return {
        "imported": report.imported,
        "skipped": report.skipped,
        "failed": report.failed,
        "activities": report.activities,
        "samples": report.records,
        "summary": report.summary(),
    }


def run_worker(settings: Settings | None = None) -> None:
    """Entry point for `garmin-mcp worker` and the Docker service."""
    worker = IngestWorker(settings or get_settings())
    worker.install_signal_handlers()
    worker.run_forever()

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
from garmin_mcp.errors import (
    GarminAuthError,
    GarminError,
    GarminMcpError,
    WorkerUnavailableError,
)
from garmin_mcp.ingest.pipeline import IngestReport, import_inbox, sync_from_garmin
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

HEARTBEAT_FILENAME = "worker.alive"
REQUEST_SUFFIX = ".request.json"
RESULT_SUFFIX = ".result.json"

# Request kinds the worker answers. Both travel the same way; only the
# handler differs. Writing is routed here rather than done in the MCP
# server because the worker already holds the only credentialed session.
KIND_SYNC = "sync"
KIND_WORKOUT = "workout"
KIND_ACTIVITY = "activity"

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
    # Request kinds this worker understands. Absent on builds predating the
    # field, which is itself the signal that it is an older one.
    supports: tuple[str, ...] = ()

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
        supports=tuple(payload.get("supports") or ()),
    )


# ─── requesting a sync from another process ───────────────────────────


def request(
    kind: str,
    payload: dict[str, Any],
    settings: Settings | None = None,
    *,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Hand the worker a job and wait for its answer.

    Raises `WorkerUnavailableError` when no worker is listening, rather than
    silently leaving a request file for one that may never start.
    """
    settings = settings or get_settings()
    settings.trigger_dir.mkdir(parents=True, exist_ok=True)

    status = read_worker_status(settings)
    if not status.alive:
        raise WorkerUnavailableError(f"no ingest worker is running ({status.detail})")

    # A worker from an older build silently ignores request kinds it does not
    # know, and the caller then waits out its whole timeout before reporting
    # something misleading like "the worker may be busy". Checking first turns
    # a version mismatch into the one sentence that actually helps.
    if status.supports and kind not in status.supports:
        raise WorkerUnavailableError(
            f"the running worker does not handle {kind!r} requests "
            f"(it supports: {', '.join(status.supports) or 'sync only'})"
        )
    if not status.supports and kind != KIND_SYNC:
        raise WorkerUnavailableError(
            f"the running worker predates {kind!r} support — rebuild and "
            "restart it (`docker compose up -d --build ingest`)"
        )

    request_id = uuid.uuid4().hex[:12]
    request_path = settings.trigger_dir / f"{kind}-{request_id}{REQUEST_SUFFIX}"
    result_path = settings.trigger_dir / f"{kind}-{request_id}{RESULT_SUFFIX}"

    _write_atomic(
        request_path,
        {"requested_at": datetime.now(UTC).isoformat(), **payload},
    )
    log.info("worker.request_sent", kind=kind, request_id=request_id)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if result_path.exists():
            result = _read_json(result_path) or {}
            result_path.unlink(missing_ok=True)
            log.info("worker.request_completed", kind=kind, request_id=request_id)
            return result
        time.sleep(0.25)

    request_path.unlink(missing_ok=True)
    raise WorkerUnavailableError(
        f"the worker did not answer within {timeout_s:.0f}s — it may be busy with a long sync"
    )


def request_sync(
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Ask the worker to pull new activities."""
    return request(KIND_SYNC, {"limit": limit}, settings, timeout_s=timeout_s)


def request_activity_edit(
    payload: dict[str, Any],
    settings: Settings | None = None,
    *,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Ask the worker to modify a completed activity."""
    return request(KIND_ACTIVITY, payload, settings, timeout_s=timeout_s)


def request_workout(
    payload: dict[str, Any],
    settings: Settings | None = None,
    *,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Ask the worker to act on the Garmin workout library.

    `payload["action"]` is list, create or delete; anything else defaults to
    create, which is what earlier builds sent.
    """
    return request(KIND_WORKOUT, payload, settings, timeout_s=timeout_s)


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

    def _handle_activity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Edit a completed activity — currently only its Notes field."""
        from garmin_mcp.garmin import workout_writer

        finished = datetime.now(UTC).isoformat()
        try:
            workout_writer.set_activity_notes(
                int(payload["activity_id"]), str(payload.get("notes", "")), self.settings
            )
        except GarminMcpError as exc:
            log.warning("worker.activity_edit_failed", error=str(exc))
            return {"finished_at": finished, "error": exc.message, "hint": exc.hint}
        except Exception as exc:
            log.exception("worker.activity_edit_failed")
            return {"finished_at": finished, "error": str(exc)}
        return {"finished_at": finished, "activity_id": int(payload["activity_id"])}

    # ─── trigger files ────────────────────────────────────────────────

    def _pending_requests(self) -> list[Path]:
        requests: list[Path] = []
        for kind in (KIND_SYNC, KIND_WORKOUT, KIND_ACTIVITY):
            requests += self.settings.trigger_dir.glob(f"{kind}-*{REQUEST_SUFFIX}")
        return sorted(requests)

    def _serve_requests(self) -> None:
        for request_path in self._pending_requests():
            payload = _read_json(request_path) or {}
            request_path.unlink(missing_ok=True)

            kind, _, remainder = request_path.name.partition("-")
            request_id = remainder[: -len(REQUEST_SUFFIX)]
            log.info("worker.serving_request", kind=kind, request_id=request_id)

            if kind == KIND_WORKOUT:
                result = self._handle_workout(payload)
            elif kind == KIND_ACTIVITY:
                result = self._handle_activity(payload)
            else:
                result = self._run_cycle(limit=payload.get("limit"))
                self._last_sync = datetime.now(UTC)

            _write_atomic(
                self.settings.trigger_dir / f"{kind}-{request_id}{RESULT_SUFFIX}",
                result,
            )
            self._heartbeat()

    def _handle_workout(self, payload: dict[str, Any]) -> dict[str, Any]:
        """List, create or delete a workout, reporting failure as data.

        Runs here rather than in the MCP server because the worker holds the
        only credentialed session — including for the read, so that listing
        does not become a reason to give the server credentials.
        """
        from garmin_mcp.garmin import workout_writer
        from garmin_mcp.garmin.workouts import spec_from_dict

        action = payload.get("action", "create")
        finished = datetime.now(UTC).isoformat()

        try:
            if action == "list":
                return {
                    "finished_at": finished,
                    "workouts": workout_writer.list_workouts(
                        self.settings, limit=int(payload.get("limit", 20))
                    ),
                }
            if action == "delete":
                workout_writer.delete_workout(int(payload["workout_id"]), self.settings)
                return {"finished_at": finished, "deleted": int(payload["workout_id"])}
            if action == "list_scheduled":
                return {
                    "finished_at": finished,
                    "scheduled": workout_writer.list_scheduled(
                        int(payload["year"]), int(payload["month"]), self.settings
                    ),
                }
            if action == "unschedule":
                workout_writer.unschedule(int(payload["schedule_id"]), self.settings)
                return {
                    "finished_at": finished,
                    "unscheduled": int(payload["schedule_id"]),
                }
            if action == "schedule":
                workout_writer.schedule_workout(
                    int(payload["workout_id"]), str(payload["date"]), self.settings
                )
                return {
                    "finished_at": finished,
                    "scheduled": int(payload["workout_id"]),
                    "date": payload["date"],
                }

            spec = spec_from_dict(payload.get("spec") or {})
            created = workout_writer.create_workout(spec, self.settings)
            return {
                "finished_at": finished,
                "created": created.as_dict(),
                "structure": spec.describe(),
            }
        except GarminMcpError as exc:
            log.warning("worker.workout_failed", action=action, error=str(exc))
            return {"finished_at": finished, "error": exc.message, "hint": exc.hint}
        except Exception as exc:
            log.exception("worker.workout_failed", action=action)
            return {"finished_at": finished, "error": str(exc)}

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
                "supports": [KIND_SYNC, KIND_WORKOUT, KIND_ACTIVITY],
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

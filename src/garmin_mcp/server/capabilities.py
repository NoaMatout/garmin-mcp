"""`get_capabilities`: what this installation can do, and what it has actually proven.

A product built on this server needs to know two different things and must not
confuse them:

* **Implemented** — the code for it exists and is switched on.
* **Verified** — something real has succeeded recently, and when.

The first is read from configuration. The second is read from what the ingest
worker observed (`ingest/sync_state.py`), because the server itself never
contacts Garmin: it holds no credentials and, by design, cannot log in. So this
tool makes no network call and cannot be blocked by an expired Garmin session —
local reads are reported as available precisely because they do not depend on
one.

Nothing secret is reported: no credential, token, cookie or session path, and
no filesystem path at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.db import queries
from garmin_mcp.db.connection import reading
from garmin_mcp.errors import DatabaseLockedError
from garmin_mcp.ingest import sync_state
from garmin_mcp.ingest.worker import WorkerStatus, read_worker_status
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

# How long past one sync interval before the data is called stale: room for a
# slow sync and one missed cycle, not for a day of silence.
_STALE_AFTER_INTERVALS = 2.0

_RECOVERY: dict[str, str] = {
    "database_missing": "run `garmin-mcp init-db`, then `garmin-mcp sync` "
    "(or drop FIT files in data/inbox/ and run `garmin-mcp import`)",
    "database_locked": "an ingest run holds the database; retry in a few seconds",
    "worker_not_running": "start it with `docker compose up -d ingest` or `garmin-mcp worker`; "
    "reading the local history does not need it",
    "garmin_auth_required": "run `garmin-mcp auth` to log in again (interactive, handles MFA); "
    "the local history stays readable meanwhile",
    "garmin_blocked": "Garmin refused the client; set GARMIN_BACKEND=playwright, or import FIT "
    "files by hand into data/inbox/",
    "garmin_rate_limited": "Garmin is rate limiting this client; wait before the next sync",
    "garmin_unavailable": "Garmin could not be reached; the next sync cycle retries on its own",
    "garmin_failed": "the last sync failed for an unclassified reason; see the worker logs",
    "data_stale": "trigger a sync with sync_now, or check that the worker is running",
    "courses_sync_failing": "course sync failed on its last attempt; activities are unaffected",
    "writes_disabled": "set GARMIN_ENABLE_WRITES=true in .env and restart the worker to allow "
    "creating workouts and notes",
}

_GARMIN_FAILURE_CODES = {
    "auth_required": "garmin_auth_required",
    "blocked": "garmin_blocked",
    "rate_limited": "garmin_rate_limited",
    "unavailable": "garmin_unavailable",
    "failed": "garmin_failed",
}

_WRITE_OPERATIONS = ("workout.create", "workout.delete", "workout.schedule", "workout.unschedule")


def _settings() -> Settings:
    return get_settings()


def _version() -> str:
    try:
        return version("garmin-mcp")
    except PackageNotFoundError:
        return "0.0.0+dev"


def _age_s(stamp: str | None, now: datetime) -> int | None:
    if not stamp:
        return None
    try:
        return round((now - datetime.fromisoformat(stamp)).total_seconds())
    except ValueError:
        return None


def _verification(
    state: str, basis: str, observed_at: str | None = None, last_ok_at: str | None = None
) -> dict[str, Any]:
    """How much trust a capability has earned.

    `verified_ok`      something real succeeded; `observed_at` says when
    `verified_failing` the last real attempt failed
    `not_verified`     implemented, never exercised on this installation
    `not_applicable`   a local computation: no Garmin access involved
    """
    entry: dict[str, Any] = {"state": state, "basis": basis}
    if observed_at:
        entry["observed_at"] = observed_at
    if last_ok_at and last_ok_at != observed_at:
        entry["last_ok_at"] = last_ok_at
    return entry


def _from_component(state: dict[str, Any], name: str) -> dict[str, Any]:
    comp = state["components"].get(name)
    if comp is None:
        return _verification("not_verified", "the worker has not completed a cycle yet")
    return _verification(
        "verified_ok" if comp["ok"] else "verified_failing",
        f"outcome of the worker's last {name} sync",
        comp["observed_at"],
        comp.get("last_ok_at"),
    )


def _from_operations(state: dict[str, Any], operations: tuple[str, ...]) -> dict[str, Any]:
    seen = [state["operations"][op] for op in operations if op in state["operations"]]
    if not seen:
        return _verification("not_verified", "no such request has been served by the worker yet")
    last_ok = max((e["last_ok_at"] for e in seen if e["last_ok_at"]), default=None)
    last_err = max((e["last_error_at"] for e in seen if e["last_error_at"]), default=None)
    if last_ok and (not last_err or last_ok >= last_err):
        return _verification("verified_ok", "a request of this kind succeeded", last_ok, last_ok)
    return _verification(
        "verified_failing", "the last request of this kind failed", last_err, last_ok
    )


def _capability(
    capability_id: str,
    description: str,
    *,
    implemented: bool,
    enabled: bool = True,
    available_now: bool | None,
    needs: list[str],
    verification: dict[str, Any],
    note: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": capability_id,
        "description": description,
        "implemented": implemented,
        "enabled": enabled,
        "available_now": available_now,
        "needs": needs,
        "verification": verification,
    }
    if note:
        entry["note"] = note
    return entry


def get_capabilities() -> dict[str, Any]:
    """What this installation can do right now, and what has actually been verified.

    Takes no arguments and makes no network call: it combines this server's
    configuration, a quick look at the local database, and what the ingest
    worker last observed about Garmin (with the date it observed it).

    Read these sections:

    * `local_history` — whether past activities can be read. Independent of
      Garmin: an expired Garmin session does not affect it.
    * `sync` and `garmin_access` — when data last arrived and what the worker
      last saw from Garmin. `garmin_access.status` is "unknown" until the worker
      has completed a cycle; `ok` is an observation with a date, not a promise.
    * `capabilities` — every feature with `implemented` (the code exists and is
      on) kept apart from `verification` (something real succeeded, and when).
    * `issues` — what is wrong, each with a stable `code` and a `recovery`
      action.
    """
    settings = _settings()
    now = datetime.now(UTC)
    state = sync_state.read_state(settings)
    worker = read_worker_status(settings)
    local = _inspect_database(settings)

    garmin = state["garmin"]
    interval_s = settings.sync_interval_minutes * 60
    last_success = state["last_success_at"]
    success_age = _age_s(last_success, now)
    if success_age is None:
        freshness = "unknown"
    else:
        freshness = "fresh" if success_age <= interval_s * _STALE_AFTER_INTERVALS else "stale"

    garmin_status = garmin["status"]
    garmin_ok: bool | None = {"ok": True, "unknown": None}.get(garmin_status, False)
    # Whether a Garmin-backed feature can run: the worker must be up, and the
    # last observation must not say Garmin is refusing us.
    garmin_available = False if not worker.alive or garmin_ok is False else garmin_ok

    writes_on = settings.enable_writes
    writes_available = bool(writes_on) and garmin_available is not False and worker.alive

    capabilities = _build_capabilities(
        state=state,
        local=local,
        garmin_available=garmin_available,
        writes_on=writes_on,
        writes_available=writes_available,
    )

    result = {
        "generated_at": now.isoformat(),
        "observation_note": "Garmin status is what the ingest worker last observed, with its "
        "date. This tool never contacts Garmin.",
        "server": {
            "version": _version(),
            "transport": settings.mcp_transport.value,
            "authentication": "none: this server does not authenticate its callers "
            "(see docs/web-app-integration.md)",
            "users": "single: one athlete, one database",
        },
        "local_history": local,
        "ingest_worker": {
            "running": worker.alive,
            "heartbeat_age_s": round(worker.age_seconds)
            if worker.age_seconds is not None
            else None,
            "handles": list(worker.supports),
            **({"detail": worker.detail} if worker.detail else {}),
        },
        "sync": {
            "last_attempt_at": state["last_attempt_at"],
            "last_success_at": last_success,
            "last_success_age_s": success_age,
            "data_freshness": freshness,
            "expected_interval_s": interval_s,
        },
        "garmin_access": {
            "status": garmin_status,
            "verified": garmin_ok is True,
            "observed_at": garmin["observed_at"],
            "observation_age_s": _age_s(garmin["observed_at"], now),
            "last_ok_at": garmin["last_ok_at"],
            "error_code": garmin["error_code"],
            "session_saved": _session_saved(settings),
            "backend": settings.backend.value,
        },
        "writes": {
            "enabled": writes_on,
            "scope": "workouts (create, delete, schedule, unschedule) and activity notes; "
            "GPX import as a navigation course is not implemented",
            "note": "reflects this server's GARMIN_ENABLE_WRITES; every write also goes "
            "through the worker and a two-step confirmation",
        },
        "capabilities": capabilities,
        "issues": _issues(
            local=local,
            worker=worker,
            garmin=garmin,
            freshness=freshness,
            state=state,
            writes_on=writes_on,
        ),
    }
    log.info(
        "tool.get_capabilities",
        local_readable=local["readable"],
        worker=worker.alive,
        garmin=garmin_status,
    )
    return result


def _session_saved(settings: Settings) -> bool:
    """Whether a Garmin session exists on disk — never its location or content."""
    try:
        return settings.token_dir.is_dir() and any(settings.token_dir.iterdir())
    except OSError:
        return False


def _inspect_database(settings: Settings) -> dict[str, Any]:
    """A quick, read-only look at the history. Never raises."""
    if not settings.db_path.exists():
        return {"readable": False, "reason": "database_missing"}
    try:
        with reading(settings, retries=2, base_delay=0.1) as conn:
            counts = queries.activity_counts(conn)
            with_gps = queries.count_activities_with_gps(conn)
            courses = queries.count_courses(conn)
            schema = queries.schema_version(conn)
    except DatabaseLockedError:
        return {"readable": False, "reason": "database_locked"}
    except Exception:
        log.warning("capabilities.database_unreadable", exc_info=True)
        return {"readable": False, "reason": "database_unreadable"}

    def _day(value: Any) -> str | None:
        return value.strftime("%Y-%m-%d") if isinstance(value, datetime) else None

    return {
        "readable": True,
        "activities": counts["activities"],
        "activities_with_start_position": with_gps,
        "courses": courses,
        "first_activity": _day(counts["first_activity"]),
        "last_activity": _day(counts["last_activity"]),
        "schema_version": schema,
    }


def _build_capabilities(
    *,
    state: dict[str, Any],
    local: dict[str, Any],
    garmin_available: bool | None,
    writes_on: bool,
    writes_available: bool,
) -> list[dict[str, Any]]:
    readable = bool(local["readable"])
    local_check = (
        _verification("verified_ok", "the database was read just now")
        if readable
        else _verification("verified_failing", "the database could not be read just now")
    )
    local_only = _verification("not_applicable", "computed locally from stored data")

    needs_local = ["local database"]
    needs_worker = ["ingest worker", "saved Garmin session"]

    return [
        _capability(
            "history.read",
            "list and read recorded activities, laps, streams, sleep, strength sets",
            implemented=True,
            available_now=readable,
            needs=needs_local,
            verification=local_check,
        ),
        _capability(
            "history.search_by_location",
            "find recorded runs that started near a point (find_local_activities)",
            implemented=True,
            available_now=readable,
            needs=needs_local,
            verification=local_check,
        ),
        _capability(
            "history.route_geometry",
            "a recorded activity's GPS track, simplified for a map (get_activity_route)",
            implemented=True,
            available_now=readable,
            needs=needs_local,
            verification=local_check,
        ),
        _capability(
            "history.export_gpx",
            "full-resolution GPX of a recorded activity, as an MCP resource",
            implemented=True,
            available_now=readable,
            needs=needs_local,
            verification=local_only,
        ),
        _capability(
            "sync.activities",
            "pull new activities from Garmin Connect",
            implemented=True,
            available_now=garmin_available,
            needs=needs_worker,
            verification=_from_component(state, "activities"),
        ),
        _capability(
            "sync.sleep",
            "pull nightly sleep from Garmin Connect",
            implemented=True,
            available_now=garmin_available,
            needs=needs_worker,
            verification=_from_component(state, "sleep"),
        ),
        _capability(
            "courses.read",
            "list saved Garmin navigation courses and their geometry",
            implemented=True,
            available_now=readable,
            needs=[*needs_local, "course sync (worker)"],
            verification=_from_component(state, "courses"),
            note="reads the local copy; the copy is refreshed from Garmin's undocumented "
            "course-service endpoint (see docs/garmin-courses.md)",
        ),
        _capability(
            "courses.export_gpx",
            "GPX of a saved course from its stored geometry, as an MCP resource",
            implemented=True,
            available_now=readable,
            needs=needs_local,
            verification=local_only,
        ),
        _capability(
            "courses.import_gpx",
            "create a navigation course in Garmin Connect from a GPX file",
            implemented=False,
            enabled=False,
            available_now=False,
            needs=[],
            verification=_verification("not_verified", "not implemented"),
            note="no documented endpoint and none in the installed client; "
            "see docs/garmin-courses.md. Fallback: export the GPX and import it by hand "
            "in Garmin Connect",
        ),
        _capability(
            "workouts.read",
            "list workouts in the Garmin library and the calendar",
            implemented=True,
            available_now=garmin_available,
            needs=needs_worker,
            verification=_from_operations(state, ("workout.list", "workout.list_scheduled")),
        ),
        _capability(
            "workouts.write",
            "create, delete, schedule and unschedule structured workouts",
            implemented=True,
            enabled=writes_on,
            available_now=writes_available,
            needs=[*needs_worker, "GARMIN_ENABLE_WRITES=true"],
            verification=_from_operations(state, _WRITE_OPERATIONS),
        ),
        _capability(
            "activities.write_notes",
            "write the Notes field of a completed activity",
            implemented=True,
            enabled=writes_on,
            available_now=writes_available,
            needs=[*needs_worker, "GARMIN_ENABLE_WRITES=true"],
            verification=_from_operations(state, ("activity.set_notes",)),
        ),
    ]


def _issues(
    *,
    local: dict[str, Any],
    worker: WorkerStatus,
    garmin: dict[str, Any],
    freshness: str,
    state: dict[str, Any],
    writes_on: bool,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(code: str, severity: str, component: str, message: str) -> None:
        issues.append(
            {
                "code": code,
                "severity": severity,
                "component": component,
                "message": message,
                "recovery": _RECOVERY[code],
            }
        )

    if not local["readable"]:
        if local.get("reason") == "database_locked":
            add("database_locked", "warning", "local_history", "the database is being written")
        else:
            add("database_missing", "error", "local_history", "no readable local database")

    if not worker.alive:
        add(
            "worker_not_running",
            "warning",
            "ingest_worker",
            "no ingest worker is running: nothing new will arrive and Garmin-backed "
            "features are unavailable",
        )

    failure = _GARMIN_FAILURE_CODES.get(garmin["status"])
    if failure:
        add(
            failure,
            "error",
            "garmin_access",
            f"the worker's last contact with Garmin failed ({garmin['status']}); "
            "reads of the local history are unaffected",
        )

    if freshness == "stale":
        add("data_stale", "warning", "sync", "no successful sync within the expected window")

    courses = state["components"].get("courses")
    if courses is not None and not courses["ok"]:
        add("courses_sync_failing", "warning", "courses", "the last course sync failed")

    if not writes_on:
        add("writes_disabled", "info", "writes", "writing to Garmin is switched off")

    return issues

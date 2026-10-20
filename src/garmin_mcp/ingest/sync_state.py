"""What the worker has actually observed about Garmin, kept across restarts.

The MCP server never talks to Garmin, so it cannot know whether Garmin is
reachable. It can only report what the one process that does talk to Garmin saw
the last time it looked, and when. This module is that record: the worker writes
it after every sync cycle and every request it serves, the server reads it.

Three deliberate limits:

* **Observations, not probes.** Nothing here contacts Garmin. A status is the
  outcome of work that was going to happen anyway, stamped with its date, so a
  reader can tell "verified ten minutes ago" from "verified last week".
* **No messages, no paths, no payloads.** Only a stable error code and a
  timestamp are stored. Garmin's own error text can echo URLs and response
  fragments, and a status file is read by a tool whose output goes to a model.
* **A failure does not erase a success.** `last_ok_at` survives the failures
  after it, which is what makes "worked until Tuesday, failing since" readable.

The file is advisory. Losing it loses history, never data: the worker simply
starts a fresh one on its next cycle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

STATE_FILENAME = "garmin-state.json"
STATE_VERSION = 1

# Error code -> the Garmin access status it implies. Anything not listed is a
# failure we cannot classify, reported as plain "failed".
_STATUS_BY_CODE = {
    "garmin_auth_required": "auth_required",
    "garmin_blocked": "blocked",
    "garmin_rate_limited": "rate_limited",
    "garmin_unavailable": "unavailable",
}

# The parts of a sync cycle, and the outcome keys each one reports through.
# (component, success key, error key)
_COMPONENTS = (
    ("activities", "garmin", "garmin_error"),
    ("sleep", "sleep", "sleep_error"),
    ("courses", "courses", "courses_error"),
)


def _path(settings: Settings) -> Path:
    return settings.state_dir / STATE_FILENAME


def _empty() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_attempt_at": None,
        "last_success_at": None,
        "garmin": {
            "status": "unknown",
            "observed_at": None,
            "last_ok_at": None,
            "error_code": None,
        },
        "components": {},
        "operations": {},
    }


def read_state(settings: Settings | None = None) -> dict[str, Any]:
    """The recorded observations, or an empty record if there are none yet.

    Never raises: a missing or unreadable file means "nothing observed".
    """
    settings = settings or get_settings()
    try:
        raw = json.loads(_path(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return _empty()
    return {**_empty(), **raw}


def _write(settings: Settings, state: dict[str, Any]) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _observe_garmin(state: dict[str, Any], *, ok: bool, code: str | None, at: str) -> None:
    garmin = state["garmin"]
    garmin["observed_at"] = at
    if ok:
        garmin["status"] = "ok"
        garmin["error_code"] = None
        garmin["last_ok_at"] = at
    else:
        garmin["status"] = _STATUS_BY_CODE.get(code or "", "failed")
        garmin["error_code"] = code


def record_cycle(settings: Settings | None, outcome: dict[str, Any]) -> None:
    """Fold one sync cycle's outcome into the record.

    `outcome` is the dict `IngestWorker._run_cycle` builds. A component that
    did not run (sleep and courses are skipped when the activity sync already
    failed) is left exactly as it was: not running is not an observation.
    """
    settings = settings or get_settings()
    try:
        state = read_state(settings)
        now = _now()
        state["last_attempt_at"] = now

        for name, ok_key, error_key in _COMPONENTS:
            if error_key in outcome:
                code = outcome.get(f"{error_key}_code") or "sync_failed"
                previous = state["components"].get(name, {})
                state["components"][name] = {
                    "ok": False,
                    "observed_at": now,
                    "error_code": code,
                    "last_ok_at": previous.get("last_ok_at"),
                }
            elif ok_key in outcome:
                state["components"][name] = {
                    "ok": True,
                    "observed_at": now,
                    "error_code": None,
                    "last_ok_at": now,
                }

        activities = state["components"].get("activities")
        if activities is not None and activities["observed_at"] == now:
            _observe_garmin(state, ok=activities["ok"], code=activities["error_code"], at=now)
            if activities["ok"]:
                state["last_success_at"] = now

        _write(settings, state)
    except Exception:  # an advisory file must never take the worker down
        log.warning("sync_state.record_failed", exc_info=True)


def record_operation(
    settings: Settings | None,
    operation: str,
    *,
    ok: bool,
    error_code: str | None = None,
) -> None:
    """Record the outcome of one request the worker served, e.g. `workout.create`.

    A request that reached Garmin and succeeded also proves access, so it
    refreshes the Garmin status the same way a sync does. A failure refreshes
    it only when the code says something about access (auth, blocked...);
    "this workout was invalid" is not news about Garmin.
    """
    settings = settings or get_settings()
    try:
        state = read_state(settings)
        now = _now()
        previous = state["operations"].get(operation, {})
        entry = {
            "last_ok_at": previous.get("last_ok_at"),
            "last_error_at": previous.get("last_error_at"),
            "last_error_code": previous.get("last_error_code"),
        }
        if ok:
            entry["last_ok_at"] = now
            _observe_garmin(state, ok=True, code=None, at=now)
        else:
            entry["last_error_at"] = now
            entry["last_error_code"] = error_code or "request_failed"
            if error_code in _STATUS_BY_CODE:
                _observe_garmin(state, ok=False, code=error_code, at=now)
        state["operations"][operation] = entry
        _write(settings, state)
    except Exception:
        log.warning("sync_state.record_failed", exc_info=True)

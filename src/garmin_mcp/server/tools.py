"""The five MCP tools.

There is deliberately no generic SQL tool. Handing a language model arbitrary
query access to a personal training database is a liability rather than a
feature: it can be talked into reading anything the file contains, and it will
occasionally write a query that scans a million rows to answer a question about
last Tuesday. Five typed tools cover the questions actually worth asking, and
every one of them is bounded.

Each tool opens a short-lived read-only connection. The ingest worker holds the
only write lock, and DuckDB permits one or the other — so grabbing the file
for the duration of a query and releasing it immediately is what keeps the two
processes out of each other's way.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.db import queries
from garmin_mcp.db.connection import reading
from garmin_mcp.errors import ActivityNotFoundError, DatabaseLockedError
from garmin_mcp.logging import get_logger
from garmin_mcp.server import formatters

log = get_logger(__name__)

# Ceilings, not suggestions. Without them one question can fill a context
# window and crowd out the conversation it was meant to inform.
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 20
MAX_STREAM_POINTS = 2000
DEFAULT_STREAM_POINTS = 200
MAX_LAPS_RETURNED = 50
MAX_SYNC_LIMIT = 200
# Generous: a first backfill downloads and parses dozens of files, and a
# timeout here would leave the work running with nobody reading the result.
SYNC_TIMEOUT_S = 180.0


def _parse_date(value: str | None, *, field: str) -> date | None:
    """Accept an ISO date, and explain precisely what is wrong otherwise."""
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date such as 2026-03-15, got {value!r}") from exc


def _settings() -> Settings:
    return get_settings()


def list_activities(
    since: str | None = None,
    activity_type: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    until: str | None = None,
) -> dict[str, Any]:
    """List recorded activities, most recent first.

    Args:
        since: Only activities on or after this ISO date (e.g. "2026-03-01").
        activity_type: Filter by sport — running, cycling, swimming,
            transition, multisport. Filtering by sport also reveals the legs
            inside multisport events, so asking for running includes the run
            of a triathlon.
        limit: Maximum activities to return (default 20, capped at 200).
        until: Only activities before this ISO date.

    Returns a compact record per activity. Multisport events appear as a single
    entry unless a sport filter is given.
    """
    limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    start = _parse_date(since, field="since")
    end = _parse_date(until, field="until")

    with reading(_settings()) as conn:
        rows = queries.list_activities(
            conn, since=start, until=end, sport=activity_type, limit=limit
        )

    log.info("tool.list_activities", returned=len(rows), sport=activity_type)
    return {
        "count": len(rows),
        "filters": {
            k: v
            for k, v in {"since": since, "until": until, "activity_type": activity_type}.items()
            if v
        },
        "activities": [formatters.summarize_activity(row) for row in rows],
    }


def get_activity_detail(activity_id: int) -> dict[str, Any]:
    """Full detail for one activity: summary, laps, and multisport legs.

    Args:
        activity_id: The activity's id, as returned by list_activities.

    Laps are what make an interval session legible — eight quarter-mile
    repeats look identical to a steady run in the summary numbers alone.
    """
    activity_id = int(activity_id)

    with reading(_settings()) as conn:
        row = queries.get_activity(conn, activity_id)
        if row is None:
            raise ActivityNotFoundError(activity_id)

        laps = queries.get_laps(conn, activity_id, limit=MAX_LAPS_RETURNED)
        total_laps = queries.count_laps(conn, activity_id)
        sample_count = queries.count_records(conn, activity_id)
        legs = queries.get_child_activities(conn, activity_id)

    detail: dict[str, Any] = {
        "activity": formatters.summarize_activity(row, verbose=True),
        "sample_count": sample_count,
    }

    if legs:
        detail["legs"] = [formatters.summarize_activity(leg, verbose=True) for leg in legs]
        detail["note"] = "this is a multisport event; each leg can also be queried by its own id"

    if laps:
        detail["laps"] = [formatters.summarize_lap(lap, row.get("sport")) for lap in laps]
        if total_laps > len(laps):
            detail["laps_truncated"] = f"showing the first {len(laps)} of {total_laps} laps"

    log.info("tool.get_activity_detail", activity_id=activity_id, laps=len(laps))
    return detail


def get_activity_streams(
    activity_id: int,
    fields: list[str] | None = None,
    max_points: int = DEFAULT_STREAM_POINTS,
) -> dict[str, Any]:
    """Time series for one activity — heart rate, pace, altitude and so on.

    Args:
        activity_id: The activity's id.
        fields: Which series to return. Defaults to heart_rate, speed and
            altitude. Available: heart_rate, speed, altitude, cadence, power,
            distance, temperature, grade, lat, lon, vertical_oscillation,
            stance_time, step_length, respiration_rate.
        max_points: How many points per series (default 200, capped at 2000).

    Series are averaged into buckets rather than returned raw — a three-hour
    ride holds around 11 000 samples per channel. Output is columnar:
    {"heart_rate": [...], "elapsed_s": [...]}.

    Because the series is smoothed, its own highest and lowest values
    understate the real ones. Read peaks from `true_range`, which is
    computed over every sample.
    """
    activity_id = int(activity_id)
    # Floor of 2 rather than something larger: a caller asking for a coarse
    # overview should get one, not a silently inflated series.
    max_points = max(2, min(int(max_points), MAX_STREAM_POINTS))
    requested = fields or ["heart_rate", "speed", "altitude"]

    unknown = [f for f in requested if f not in queries.STREAM_FIELDS]
    if unknown:
        raise ValueError(f"unknown fields {unknown}; available: {sorted(queries.STREAM_FIELDS)}")

    with reading(_settings()) as conn:
        if queries.get_activity(conn, activity_id) is None:
            raise ActivityNotFoundError(activity_id)
        total = queries.count_records(conn, activity_id)
        streams = queries.get_streams(conn, activity_id, requested, max_points=max_points)
        extrema = queries.stream_extrema(conn, activity_id, requested)

    returned = len(streams.get("elapsed_s", []))
    if total == 0:
        return {
            "activity_id": activity_id,
            "points": 0,
            "series": {},
            "note": "this activity has no per-second samples — only summary data",
        }

    log.info("tool.get_activity_streams", activity_id=activity_id, points=returned)
    return formatters.format_streams(
        streams,
        activity_id=activity_id,
        total_samples=total,
        returned=returned,
        extrema=extrema,
    )


def weekly_summary(week_start: str | None = None) -> dict[str, Any]:
    """Training totals for one week, broken down by sport.

    Args:
        week_start: Any ISO date within the week of interest. Snapped back to
            the Monday, so "2026-03-18" and "2026-03-16" describe the same
            week. Defaults to the current week.

    Multisport events count once, at their combined distance, rather than once
    per leg.
    """
    requested = _parse_date(week_start, field="week_start") or date.today()
    monday = requested - timedelta(days=requested.weekday())

    with reading(_settings()) as conn:
        rows = queries.weekly_summary(conn, monday)
        sessions = queries.week_activities(conn, monday)

    summary = formatters.summarize_week(rows, monday)
    summary["activities"] = [formatters.summarize_activity(s) for s in sessions]

    if not rows:
        summary["note"] = "no activities recorded in this week"

    log.info("tool.weekly_summary", week_start=monday.isoformat(), sports=len(rows))
    return summary


def compare_activities(id_a: int, id_b: int) -> dict[str, Any]:
    """Compare two activities side by side, with the differences computed.

    Args:
        id_a: First activity id.
        id_b: Second activity id.

    Deltas are calculated here rather than left to the reader, including a
    plain-language verdict on pace — where a positive number means slower,
    which is easy to misread.
    """
    id_a, id_b = int(id_a), int(id_b)
    if id_a == id_b:
        raise ValueError("id_a and id_b are the same activity")

    with reading(_settings()) as conn:
        left = queries.get_activity(conn, id_a)
        if left is None:
            raise ActivityNotFoundError(id_a)
        right = queries.get_activity(conn, id_b)
        if right is None:
            raise ActivityNotFoundError(id_b)

    result = formatters.compare(left, right)

    if left.get("sport") != right.get("sport"):
        result["warning"] = (
            f"different sports ({left.get('sport')} vs {right.get('sport')}) — "
            "pace and power are not comparable across them"
        )

    log.info("tool.compare_activities", id_a=id_a, id_b=id_b)
    return result


def database_status() -> dict[str, Any]:
    """What the database currently holds, and whether it is reachable.

    Worth calling before concluding that an activity is missing: an empty
    result may mean nothing has been ingested yet rather than that the session
    never happened.
    """
    settings = _settings()
    if not settings.db_path.exists():
        return {
            "available": False,
            "reason": f"no database at {settings.db_path}",
            "fix": "run `garmin-mcp init-db`, then `garmin-mcp sync` or drop FIT "
            "files into data/inbox/ and run `garmin-mcp import`",
        }

    try:
        with reading(settings) as conn:
            counts = queries.activity_counts(conn)
            sports = queries.volume_by_sport(conn)
    except DatabaseLockedError as exc:
        return {"available": False, "reason": str(exc)}

    def _stamp(value: Any) -> str | None:
        return value.strftime("%Y-%m-%d") if isinstance(value, datetime | date) else None

    from garmin_mcp.ingest.worker import read_worker_status

    worker = read_worker_status(settings)

    return {
        "available": True,
        "sync_worker": (
            {"running": True, "last_sync": worker.last_sync}
            if worker.alive
            else {
                "running": False,
                "detail": worker.detail,
                "note": "automatic sync is off; use `garmin-mcp sync` manually",
            }
        ),
        "activities": counts["activities"],
        "samples": counts["records"],
        "files_parsed": counts["files_parsed"],
        "files_failed": counts["files_failed"],
        "first_activity": _stamp(counts["first_activity"]),
        "last_activity": _stamp(counts["last_activity"]),
        "by_sport": [
            {"sport": sport, "activities": count, "distance_km": round(km, 1)}
            for sport, count, km in sports
        ],
    }


def sync_now(limit: int | None = None) -> dict[str, Any]:
    """Pull new activities from Garmin right now, without leaving the conversation.

    Args:
        limit: Maximum activities to download (default: the configured batch
            size, 25).

    Requires the ingest worker to be running — it is the only process allowed
    to write, since DuckDB grants exclusive access to a single writer. If no
    worker is listening this fails immediately with instructions, rather than
    appearing to hang.

    Also imports anything waiting in data/inbox/, which needs neither network
    nor credentials.
    """
    from garmin_mcp.ingest.worker import request_sync

    if limit is not None:
        limit = max(1, min(int(limit), MAX_SYNC_LIMIT))

    result = request_sync(_settings(), limit=limit, timeout_s=SYNC_TIMEOUT_S)

    summary: dict[str, Any] = {"finished_at": result.get("finished_at")}

    inbox = result.get("inbox") or {}
    if inbox.get("imported") or inbox.get("failed"):
        summary["inbox"] = inbox.get("summary")

    garmin = result.get("garmin") or {}
    if garmin:
        summary["garmin"] = garmin.get("summary")
        summary["new_activities"] = garmin.get("activities", 0)

    # Surface a failed sync as content rather than swallowing it: an empty
    # result and a broken Garmin session look identical otherwise.
    for key in ("garmin_error", "garmin_hint", "inbox_error"):
        if result.get(key):
            summary[key] = result[key]

    if not garmin and not inbox:
        summary["note"] = "nothing new to ingest"

    log.info("tool.sync_now", new=summary.get("new_activities"))
    return summary


# ══════════════════════════════════════════════════════════════════════
# Writing back to Garmin.
#
# Everything above reads. These two modify the athlete's account, and are
# built to a different standard: disabled unless explicitly switched on,
# never acting on a single call, and undoable.
# ══════════════════════════════════════════════════════════════════════


def create_workout(
    name: str,
    blocks: list[dict[str, Any]],
    sport: str = "running",
    description: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a structured workout in the athlete's Garmin library.

    Args:
        name: What the session is called, e.g. "Threshold 4x2km".
        blocks: The session, in order. A block is either a step or a repeat.
            Step: {"kind": "warmup"|"interval"|"recovery"|"cooldown"|"rest",
                   "duration_s": 1200}  or  {"distance_m": 2000},
                   optionally "target_pace": "3:55", "pace_tolerance_s": 5,
                   and "alert": true|false.
                   Exactly one of duration_s or distance_m per step.
            Repeat: {"times": 4, "steps": [ ...steps... ]}
        sport: running, cycling or swimming.
        description: Optional note attached to the workout.
        confirm: Must be true to actually create it.

    **Call this without `confirm` first.** That returns the session written out
    in full and creates nothing. Show it to the athlete, and only call again
    with confirm=true once they have agreed — this puts a real workout on their
    Garmin account and onto their watch, and they should see it before that
    happens rather than after.

    **Ask about pace alerts whenever a step has a target and the athlete has
    not said.** With `"alert": true` (the default) the watch beeps whenever
    they drift outside the range, for every interval; with `"alert": false`
    the pace is shown but nothing sounds. It is a strong personal preference,
    it cannot be guessed from their data, and it is discovered mid-session if
    guessed wrong. The preview states which is in force.

    **Watch for ambiguous prescriptions.** "5x1km à 3:00" may mean five
    kilometres at 3:00/km or five kilometres with 3:00 of recovery. If a pace
    looks inconsistent with the athlete's recent sessions, say so and ask
    rather than putting an impossible target on their watch.

    Example blocks for "20 min easy, then 12 x 1 min at 3:45 with 1 min float":
        [{"kind": "warmup", "duration_s": 1200},
         {"times": 12, "steps": [
             {"kind": "interval", "duration_s": 60, "target_pace": "3:45"},
             {"kind": "recovery", "duration_s": 60}]},
         {"kind": "cooldown", "duration_s": 600}]
    """
    from garmin_mcp.garmin.workouts import WorkoutSpecError, spec_from_dict
    from garmin_mcp.ingest.worker import request_workout

    settings = _settings()

    # Fail before building anything if writing is switched off, so the reason
    # is the actual one rather than a confusing downstream error.
    if not settings.enable_writes:
        return {
            "created": False,
            "error": "writing to Garmin is disabled on this installation",
            "fix": "set GARMIN_ENABLE_WRITES=true in .env and restart the ingest worker",
        }

    try:
        spec = spec_from_dict(
            {"name": name, "blocks": blocks, "sport": sport, "description": description}
        )
        spec.validate()
    except WorkoutSpecError as exc:
        raise ValueError(str(exc)) from exc

    if not confirm:
        # Nothing has left the machine at this point: the spec was built and
        # validated locally, with no credential involved.
        return {
            "created": False,
            "preview": spec.summary(),
            "next_step": (
                "show this session to the athlete; if they agree, call "
                "create_workout again with the same arguments and confirm=true"
            ),
        }

    result = request_workout(
        {
            "action": "create",
            "spec": {
                "name": name,
                "blocks": blocks,
                "sport": sport,
                "description": description,
            },
        },
        settings,
    )

    if result.get("error"):
        return {"created": False, "error": result["error"], "fix": result.get("hint")}

    created = result.get("created", {})
    log.info("tool.create_workout", workout_id=created.get("workout_id"))
    return {
        "created": True,
        **created,
        "structure": result.get("structure"),
        "note": (
            "the workout is in the Garmin library and will sync to the watch; "
            "it is not scheduled on any date"
        ),
    }


def list_workouts(limit: int = 20) -> dict[str, Any]:
    """List the workouts saved in the athlete's Garmin library.

    Args:
        limit: How many to return, newest first (default 20, max 100).

    Use this to find a workout's id — `delete_workout` needs one, and an id is
    otherwise only ever visible in the conversation that created it. Reading,
    so it works even when writing is disabled.
    """
    from garmin_mcp.ingest.worker import request_workout

    limit = max(1, min(int(limit), 100))
    result = request_workout({"action": "list", "limit": limit}, _settings())

    if result.get("error"):
        return {"workouts": [], "error": result["error"], "fix": result.get("hint")}

    workouts = result.get("workouts", [])
    log.info("tool.list_workouts", count=len(workouts))
    return {
        "count": len(workouts),
        "workouts": [_compact_workout(w) for w in workouts],
    }


def _compact_workout(raw: dict[str, Any]) -> dict[str, Any]:
    from garmin_mcp.domain.units import format_duration

    entry = {
        "workout_id": raw.get("workout_id"),
        "name": raw.get("name"),
        "sport": raw.get("sport"),
        "duration": format_duration(raw.get("estimated_duration_s")),
        "updated": (raw.get("updated") or "")[:10] or None,
    }
    return {k: v for k, v in entry.items() if v is not None}


def delete_workout(workout_id: int, confirm: bool = False) -> dict[str, Any]:
    """Remove a workout from the athlete's Garmin library.

    Args:
        workout_id: The id, from list_workouts or from create_workout.
        confirm: Must be true to actually delete.

    The undo for create_workout. Same two-step rule: without confirm it
    reports what would be removed and does nothing.
    """
    from garmin_mcp.ingest.worker import request_workout

    settings = _settings()
    if not settings.enable_writes:
        return {
            "deleted": False,
            "error": "writing to Garmin is disabled on this installation",
        }

    workout_id = int(workout_id)
    if not confirm:
        return {
            "deleted": False,
            "would_delete": workout_id,
            "next_step": "call again with confirm=true to remove it",
        }

    # Through the worker, like every other Garmin call: the MCP server holds
    # no credentials and must not start doing so for the sake of one delete.
    result = request_workout({"action": "delete", "workout_id": workout_id}, settings)
    if result.get("error"):
        return {"deleted": False, "error": result["error"], "fix": result.get("hint")}

    log.info("tool.delete_workout", workout_id=workout_id)
    return {"deleted": True, "workout_id": workout_id}


def schedule_workout(workout_id: int, date: str, confirm: bool = False) -> dict[str, Any]:
    """Put a saved workout on a date in the athlete's Garmin calendar.

    Args:
        workout_id: The id, from list_workouts or create_workout.
        date: ISO date, e.g. "2026-08-19".
        confirm: Must be true to actually schedule.

    Separate from creating on purpose. A workout in the library is a
    suggestion; one on tomorrow's calendar is a plan, and the athlete should
    say which they meant.
    """
    from garmin_mcp.ingest.worker import request_workout

    settings = _settings()
    if not settings.enable_writes:
        return {
            "scheduled": False,
            "error": "writing to Garmin is disabled on this installation",
        }

    when = _parse_date(date, field="date")
    if when is None:
        raise ValueError("date is required, as an ISO date such as 2026-08-19")

    workout_id = int(workout_id)
    if not confirm:
        return {
            "scheduled": False,
            "would_schedule": {"workout_id": workout_id, "date": when.isoformat()},
            "next_step": "call again with confirm=true to put it on the calendar",
        }

    result = request_workout(
        {"action": "schedule", "workout_id": workout_id, "date": when.isoformat()},
        settings,
    )
    if result.get("error"):
        return {"scheduled": False, "error": result["error"], "fix": result.get("hint")}

    log.info("tool.schedule_workout", workout_id=workout_id, date=when.isoformat())
    return {"scheduled": True, "workout_id": workout_id, "date": when.isoformat()}


def set_activity_notes(activity_id: int, notes: str, confirm: bool = False) -> dict[str, Any]:
    """Write the Notes field of a completed activity in Garmin Connect.

    Args:
        activity_id: The activity's id.
        notes: The text to store, up to 2000 characters.
        confirm: Must be true to actually write.

    Useful for keeping an analysis attached to the session it describes,
    rather than leaving it in a conversation nobody will find again. Replaces
    whatever is already in the field, so read it back first if the athlete may
    have written something there themselves.
    """
    from garmin_mcp.ingest.worker import request_activity_edit

    settings = _settings()
    if not settings.enable_writes:
        return {
            "written": False,
            "error": "writing to Garmin is disabled on this installation",
        }

    activity_id = int(activity_id)
    text = (notes or "").strip()
    if not text:
        raise ValueError("notes cannot be empty")

    if not confirm:
        return {
            "written": False,
            "preview": text[:500] + ("…" if len(text) > 500 else ""),
            "characters": len(text),
            "warning": "this replaces the existing Notes field entirely",
            "next_step": "call again with confirm=true to write it",
        }

    result = request_activity_edit({"activity_id": activity_id, "notes": text}, settings)
    if result.get("error"):
        return {"written": False, "error": result["error"], "fix": result.get("hint")}

    log.info("tool.set_activity_notes", activity_id=activity_id, chars=len(text))
    return {"written": True, "activity_id": activity_id, "characters": len(text)}


def compare_to_plan(activity_id: int) -> dict[str, Any]:
    """Compare a completed session against the workout it was run from.

    Args:
        activity_id: The activity's id.

    Only works for sessions started from a structured workout on the watch —
    the prescription travels inside the FIT file, so the pairing between each
    lap and the step it belongs to is recorded by the device rather than
    guessed here.

    Returns the plan step by step with what was actually done: reps prescribed
    against reps completed, pace per rep, and the drift across a set, which a
    single average hides — holding 3:43 throughout and fading from 3:35 to
    3:52 average the same.
    """
    activity_id = int(activity_id)

    with reading(_settings()) as conn:
        activity = queries.get_activity(conn, activity_id)
        if activity is None:
            raise ActivityNotFoundError(activity_id)
        steps = queries.get_planned_steps(conn, activity_id)
        laps_by_step = queries.get_laps_by_step(conn, activity_id)

    if not steps:
        return {
            "activity_id": activity_id,
            "has_plan": False,
            "note": (
                "this session was not run from a structured workout, so there is "
                "nothing to compare it against — use get_activity_detail for its laps"
            ),
        }

    result = formatters.compare_to_plan(activity, steps, laps_by_step)
    result["has_plan"] = True
    log.info("tool.compare_to_plan", activity_id=activity_id, steps=len(steps))
    return result


def list_scheduled_workouts(month: str | None = None) -> dict[str, Any]:
    """List the workouts placed on the athlete's Garmin calendar.

    Args:
        month: Any ISO date in the month of interest, e.g. "2026-08-17".
            Defaults to the current month.

    Returns each planned session with its date, its `workout_id` (the session
    in the library) and its `schedule_id` (this particular placement on the
    calendar). Removing something from the calendar needs the schedule_id;
    deleting the session itself needs the workout_id.

    Call this before acting on a request like "delete next week's runs" —
    the library alone does not say what is planned, and matching on names is
    how the wrong session gets deleted.
    """
    from garmin_mcp.ingest.worker import request_workout

    anchor = _parse_date(month, field="month") or date.today()
    result = request_workout(
        {"action": "list_scheduled", "year": anchor.year, "month": anchor.month},
        _settings(),
    )
    if result.get("error"):
        return {"scheduled": [], "error": result["error"], "fix": result.get("hint")}

    scheduled = result.get("scheduled", [])
    log.info("tool.list_scheduled_workouts", month=anchor.strftime("%Y-%m"), count=len(scheduled))
    return {
        "month": anchor.strftime("%Y-%m"),
        "count": len(scheduled),
        "scheduled": [{k: v for k, v in entry.items() if v is not None} for entry in scheduled],
    }


def unschedule_workout(schedule_id: int, confirm: bool = False) -> dict[str, Any]:
    """Take a workout off the calendar, keeping it in the library.

    Args:
        schedule_id: From list_scheduled_workouts — not the workout_id.
        confirm: Must be true to actually remove it.

    Use this rather than delete_workout when the athlete wants to move or drop
    a planned session but keep the session itself.
    """
    from garmin_mcp.ingest.worker import request_workout

    settings = _settings()
    if not settings.enable_writes:
        return {
            "unscheduled": False,
            "error": "writing to Garmin is disabled on this installation",
        }

    schedule_id = int(schedule_id)
    if not confirm:
        return {
            "unscheduled": False,
            "would_remove": schedule_id,
            "note": "the workout stays in the library; only the calendar entry goes",
            "next_step": "call again with confirm=true",
        }

    result = request_workout({"action": "unschedule", "schedule_id": schedule_id}, settings)
    if result.get("error"):
        return {"unscheduled": False, "error": result["error"], "fix": result.get("hint")}

    log.info("tool.unschedule_workout", schedule_id=schedule_id)
    return {"unscheduled": True, "schedule_id": schedule_id}

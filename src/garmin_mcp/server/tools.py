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

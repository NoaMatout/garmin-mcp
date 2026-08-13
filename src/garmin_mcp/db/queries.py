"""Every SQL statement in the project lives here.

The MCP server exposes no generic SQL tool, by design: a language model with
arbitrary query access to a personal training database is a liability, not a
feature — it can be talked into reading anything, and it can write queries that
scan a million rows to answer a question about last Tuesday.

Instead each tool calls a named function here with typed parameters, and every
statement below is parameterised. Keeping the SQL in one module is what makes
that claim auditable at a glance rather than a promise.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import duckdb


def _one(result: duckdb.DuckDBPyRelation | Any) -> tuple[Any, ...]:
    """Fetch a row that an aggregate query must always produce.

    `fetchone()` is typed as optional and genuinely returns None for an empty
    result set. Indexing it directly fails with an opaque TypeError far from
    the cause, so aggregates that cannot legitimately return nothing are
    funnelled through here.
    """
    row = result.fetchone()
    if row is None:  # pragma: no cover - an aggregate always yields a row
        raise RuntimeError("aggregate query returned no row")
    return row


def latest_garmin_start(conn: duckdb.DuckDBPyConnection) -> datetime | None:
    """When the newest Garmin-sourced activity began.

    The sync watermark. Manually imported files are excluded on purpose: a
    2019 file dropped into the inbox last week must not make the next sync
    believe it is already up to date.
    """
    row = _one(conn.execute("SELECT max(start_time_utc) FROM activities WHERE source = 'garmin'"))
    return row[0] if row[0] else None


def known_garmin_ids(conn: duckdb.DuckDBPyConnection) -> set[int]:
    """Every Garmin activity id already stored, parents and legs alike."""
    rows = conn.execute(
        "SELECT DISTINCT garmin_activity_id FROM activities WHERE garmin_activity_id IS NOT NULL"
    ).fetchall()
    return {int(row[0]) for row in rows}


def failed_file_hashes(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Files that previously failed to parse.

    Used to avoid re-downloading something that will fail again. A parser
    version bump clears the way for a retry.
    """
    rows = conn.execute("SELECT file_hash FROM files WHERE status = 'failed'").fetchall()
    return {row[0] for row in rows}


def activity_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Headline numbers for `garmin-mcp info` and the server's health output."""
    activities, first, last = _one(
        conn.execute(
            """
            SELECT count(*), min(start_time_local), max(start_time_local)
            FROM activities
            """
        )
    )
    records = _one(conn.execute("SELECT count(*) FROM records"))[0]
    parsed = _one(conn.execute("SELECT count(*) FROM files WHERE status = 'parsed'"))[0]
    failed = _one(conn.execute("SELECT count(*) FROM files WHERE status = 'failed'"))[0]
    return {
        "activities": activities,
        "records": records,
        "files_parsed": parsed,
        "files_failed": failed,
        "first_activity": first,
        "last_activity": last,
    }


def volume_by_sport(
    conn: duckdb.DuckDBPyConnection,
    since: date | None = None,
) -> list[tuple[str | None, int, float]]:
    """Activity count and kilometres per sport.

    Only top-level rows are counted. Including a triathlon's legs alongside
    their parent would report the same 51.5 km twice.
    """
    sql = """
        SELECT sport, count(*) AS activities, coalesce(sum(total_distance_m), 0) / 1000
        FROM activities
        WHERE parent_activity_id IS NULL
    """
    params: list[Any] = []
    if since is not None:
        sql += " AND start_time_local >= ?"
        params.append(datetime.combine(since, datetime.min.time()))
    sql += " GROUP BY sport ORDER BY activities DESC"
    return [(row[0], int(row[1]), float(row[2])) for row in conn.execute(sql, params).fetchall()]


# ══════════════════════════════════════════════════════════════════════
# Queries backing the MCP tools.
#
# Each corresponds to exactly one tool. Every value the model supplies is
# bound as a parameter — sport names and dates included — so no tool input
# is ever concatenated into SQL.
# ══════════════════════════════════════════════════════════════════════

# Columns worth returning for a list entry. Selecting `*` would drag the JSON
# `extra` blob into every row and inflate the model's context for nothing.
_SUMMARY_COLUMNS = """
    activity_id, parent_activity_id, sport, sub_sport, name,
    start_time_local, total_timer_time_s, total_elapsed_time_s,
    total_distance_m, total_ascent_m, avg_speed_mps, avg_heart_rate,
    max_heart_rate, avg_cadence, avg_power_w, total_calories,
    aerobic_training_effect, pace_s_per_km, week_start
"""


def list_activities(
    conn: duckdb.DuckDBPyConnection,
    *,
    since: date | None = None,
    until: date | None = None,
    sport: str | None = None,
    limit: int = 20,
    include_legs: bool = False,
) -> list[dict[str, Any]]:
    """Recent activities, newest first.

    Legs of a multisport event are hidden by default: a triathlon should read
    as one line, not six. Asking for a specific sport reveals them, because
    "my runs in June" ought to include the 10 km inside a triathlon — which is
    the whole reason legs are stored separately.
    """
    sql = f"SELECT {_SUMMARY_COLUMNS} FROM v_activity_summary WHERE 1 = 1"
    params: list[Any] = []

    if not include_legs and sport is None:
        sql += " AND parent_activity_id IS NULL"
    if sport is not None:
        sql += " AND sport = ?"
        params.append(sport)
    if since is not None:
        sql += " AND start_time_local >= ?"
        params.append(datetime.combine(since, datetime.min.time()))
    if until is not None:
        sql += " AND start_time_local < ?"
        params.append(datetime.combine(until, datetime.max.time()))

    sql += " ORDER BY start_time_local DESC LIMIT ?"
    params.append(limit)

    result = conn.execute(sql, params)
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def get_activity(conn: duckdb.DuckDBPyConnection, activity_id: int) -> dict[str, Any] | None:
    """One activity's full summary row, or None when the id is unknown."""
    result = conn.execute(
        "SELECT * EXCLUDE (extra) FROM v_activity_summary WHERE activity_id = ?",
        [activity_id],
    )
    row = result.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in result.description], row, strict=True))


def get_child_activities(
    conn: duckdb.DuckDBPyConnection, parent_activity_id: int
) -> list[dict[str, Any]]:
    """The legs of a multisport event, in the order they were raced."""
    result = conn.execute(
        f"SELECT {_SUMMARY_COLUMNS} FROM v_activity_summary "
        "WHERE parent_activity_id = ? ORDER BY session_index",
        [parent_activity_id],
    )
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def get_laps(
    conn: duckdb.DuckDBPyConnection, activity_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    """Laps for an activity — the structure of an interval session."""
    result = conn.execute(
        """
        SELECT lap_index, total_timer_time_s, total_distance_m, avg_speed_mps,
               avg_heart_rate, max_heart_rate, avg_cadence, avg_power_w,
               total_ascent_m, intensity
        FROM laps WHERE activity_id = ?
        ORDER BY lap_index LIMIT ?
        """,
        [activity_id, limit],
    )
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def count_laps(conn: duckdb.DuckDBPyConnection, activity_id: int) -> int:
    return int(
        _one(conn.execute("SELECT count(*) FROM laps WHERE activity_id = ?", [activity_id]))[0]
    )


def count_records(conn: duckdb.DuckDBPyConnection, activity_id: int) -> int:
    return int(
        _one(conn.execute("SELECT count(*) FROM records WHERE activity_id = ?", [activity_id]))[0]
    )


# Stream fields a caller may request, mapped to their column. Acting as an
# allow-list matters: the field names arrive from a language model, and this
# is what stops them reaching the SQL text.
STREAM_FIELDS: dict[str, str] = {
    "heart_rate": "heart_rate",
    "speed": "speed_mps",
    "altitude": "altitude_m",
    "cadence": "cadence",
    "power": "power_w",
    "distance": "distance_m",
    "temperature": "temperature_c",
    "grade": "grade",
    "lat": "lat",
    "lon": "lon",
    "vertical_oscillation": "vertical_oscillation_mm",
    "stance_time": "stance_time_ms",
    "step_length": "step_length_mm",
    "respiration_rate": "respiration_rate",
}


def get_streams(
    conn: duckdb.DuckDBPyConnection,
    activity_id: int,
    fields: list[str],
    *,
    max_points: int = 200,
) -> dict[str, list[Any]]:
    """Down-sampled sample series, in columnar form.

    A three-hour ride holds ~11 000 samples per channel. Returned raw that is
    tens of thousands of numbers for one question, so the series is bucketed
    into at most `max_points` averages. Distance and position take the last
    value in each bucket instead of the mean — averaging a coordinate moves
    the athlete off the road, and averaging a cumulative counter is
    meaningless.

    Columnar rather than a list of objects: roughly three times fewer tokens
    for the same numbers.
    """
    unknown = [f for f in fields if f not in STREAM_FIELDS]
    if unknown:
        raise ValueError(f"unknown stream fields: {unknown}. Available: {sorted(STREAM_FIELDS)}")

    total = count_records(conn, activity_id)
    if total == 0:
        return {"elapsed_s": []} | {f: [] for f in fields}

    # One bucket per sample when the series already fits.
    bucket_size = max(1, -(-total // max_points))

    selects = ["min(elapsed_s) AS elapsed_s"]
    for field in fields:
        column = STREAM_FIELDS[field]
        if field in ("distance", "lat", "lon"):
            selects.append(f'last({column} ORDER BY elapsed_s) AS "{field}"')
        else:
            selects.append(f'avg({column}) AS "{field}"')

    result = conn.execute(
        f"""
        SELECT {", ".join(selects)}
        FROM (
            -- `//` is integer division. Plain `/` returns a DOUBLE in DuckDB,
            -- which gives almost every row its own bucket and silently
            -- defeats the down-sampling entirely.
            SELECT *, (row_number() OVER (ORDER BY ts) - 1) // ? AS bucket
            FROM records WHERE activity_id = ?
        )
        GROUP BY bucket ORDER BY bucket
        """,
        [bucket_size, activity_id],
    )

    columns = [d[0] for d in result.description]
    rows = result.fetchall()
    return {column: [row[i] for row in rows] for i, column in enumerate(columns)}


def weekly_summary(conn: duckdb.DuckDBPyConnection, week_start: date) -> list[dict[str, Any]]:
    """Per-sport totals for one week, Monday to Sunday.

    Counts top-level activities only, so a triathlon contributes its combined
    distance once rather than once per leg.
    """
    result = conn.execute(
        """
        SELECT sport,
               count(*)                                  AS activities,
               coalesce(sum(total_distance_m), 0) / 1000 AS distance_km,
               coalesce(sum(total_timer_time_s), 0)      AS moving_time_s,
               coalesce(sum(total_ascent_m), 0)          AS ascent_m,
               coalesce(sum(total_calories), 0)          AS calories,
               avg(avg_heart_rate)                       AS avg_heart_rate
        FROM v_activity_summary
        WHERE parent_activity_id IS NULL
          AND start_time_local >= ?
          AND start_time_local <  ?
        GROUP BY sport
        ORDER BY moving_time_s DESC
        """,
        [
            datetime.combine(week_start, datetime.min.time()),
            datetime.combine(week_start + timedelta(days=7), datetime.min.time()),
        ],
    )
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def week_activities(conn: duckdb.DuckDBPyConnection, week_start: date) -> list[dict[str, Any]]:
    """The individual sessions making up a week, for context under the totals."""
    return list_activities(
        conn,
        since=week_start,
        until=week_start + timedelta(days=6),
        limit=50,
    )


def stream_extrema(
    conn: duckdb.DuckDBPyConnection,
    activity_id: int,
    fields: list[str],
) -> dict[str, dict[str, float]]:
    """True min, max and mean per field, from the raw samples.

    Down-sampling averages, and averaging destroys extremes: on a real ride of
    1 513 samples, asking for a 10-point overview reported a maximum heart rate
    of 160 when the athlete actually reached 174, and a minimum of 138 against
    a true 111. A model reading only the series will state those wrong numbers
    with complete confidence.

    So the honest peaks are computed here, over every sample, and returned
    alongside the series. One extra aggregate scan removes the trap entirely.
    """
    usable = [f for f in fields if f in STREAM_FIELDS and f not in ("lat", "lon")]
    if not usable:
        return {}

    selects = []
    for field in usable:
        column = STREAM_FIELDS[field]
        selects += [
            f'min({column}) AS "{field}__min"',
            f'max({column}) AS "{field}__max"',
            f'avg({column}) AS "{field}__avg"',
        ]

    result = conn.execute(
        f"SELECT {', '.join(selects)} FROM records WHERE activity_id = ?",
        [activity_id],
    )
    row = result.fetchone()
    if row is None:
        return {}

    columns = [d[0] for d in result.description]
    flat = dict(zip(columns, row, strict=True))

    extrema: dict[str, dict[str, float]] = {}
    for field in usable:
        stats = {key: flat.get(f"{field}__{key}") for key in ("min", "max", "avg")}
        if any(value is not None for value in stats.values()):
            extrema[field] = {
                key: round(float(value), 2) for key, value in stats.items() if value is not None
            }
    return extrema


def get_planned_steps(conn: duckdb.DuckDBPyConnection, activity_id: int) -> list[dict[str, Any]]:
    """The structured workout this activity was run from, if any."""
    result = conn.execute(
        """
        SELECT step_index, workout_name, intensity, duration_type, duration_value,
               target_type, target_low, target_high, repeat_from_step, repeat_count
        FROM workout_steps WHERE activity_id = ?
        ORDER BY step_index
        """,
        [activity_id],
    )
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def get_laps_by_step(
    conn: duckdb.DuckDBPyConnection, activity_id: int
) -> dict[int, list[dict[str, Any]]]:
    """Laps grouped by the prescribed step they belong to.

    The grouping key comes from the watch, not from us, so a lap is attributed
    to the step the athlete was actually running — including when they pressed
    lap mid-interval.
    """
    result = conn.execute(
        """
        SELECT wkt_step_index, lap_index, total_timer_time_s, total_distance_m,
               avg_speed_mps, avg_heart_rate, max_heart_rate, avg_cadence,
               avg_power_w, intensity
        FROM laps
        WHERE activity_id = ? AND wkt_step_index IS NOT NULL
        ORDER BY lap_index
        """,
        [activity_id],
    )
    columns = [d[0] for d in result.description]
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in result.fetchall():
        entry = dict(zip(columns, row, strict=True))
        grouped.setdefault(int(entry["wkt_step_index"]), []).append(entry)
    return grouped

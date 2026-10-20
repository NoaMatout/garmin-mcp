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


def activity_names(conn: duckdb.DuckDBPyConnection) -> dict[int, str]:
    """Activity titles already stored, keyed by Garmin id.

    Titles are set in Garmin Connect and arrive with the activity listing, not
    inside the FIT file. Re-parsing rebuilds rows from the file alone, so they
    have to be carried across explicitly or they are lost.
    """
    rows = conn.execute(
        "SELECT garmin_activity_id, name FROM activities "
        "WHERE garmin_activity_id IS NOT NULL AND name IS NOT NULL"
    ).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


def refresh_activity_names(conn: duckdb.DuckDBPyConnection, names: dict[int, str]) -> int:
    """Store titles for activities already ingested, and report how many changed.

    The activity listing carries names and is fetched on every sync anyway, so
    keeping them current costs nothing extra. Without this, a title edited in
    Garmin Connect never reaches the database — and a re-parse that dropped
    them would need a full re-download to recover.
    """
    if not names:
        return 0

    updated = 0
    for garmin_id, name in names.items():
        result = conn.execute(
            """
            UPDATE activities SET name = ?
            WHERE garmin_activity_id = ?
              AND (name IS NULL OR name <> ?)
            """,
            [name, garmin_id, name],
        )
        updated += result.fetchall()[0][0] if result.description else 0
    return updated


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


def sleep_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Headline numbers for `database_status` — mirrors `activity_counts`."""
    nights, first, last = _one(
        conn.execute("SELECT count(*), min(calendar_date), max(calendar_date) FROM sleep_sessions")
    )
    return {"nights": nights, "first_night": first, "last_night": last}


def course_ids_with_detail(conn: duckdb.DuckDBPyConnection) -> set[int]:
    """Courses whose geometry is already stored — the expensive fetch to skip."""
    rows = conn.execute("SELECT DISTINCT course_id FROM course_points").fetchall()
    return {int(row[0]) for row in rows}


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


def list_courses(
    conn: duckdb.DuckDBPyConnection,
    *,
    sport: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Saved courses, newest first."""
    sql = """
        SELECT course_id, name, sport, distance_m, elevation_gain_m,
               elevation_loss_m, favorite, created_at,
               EXISTS (
                   SELECT 1 FROM course_points p WHERE p.course_id = c.course_id
               ) AS has_geometry
        FROM courses c WHERE 1 = 1
    """
    params: list[Any] = []
    if sport is not None:
        sql += " AND sport = ?"
        params.append(sport)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    result = conn.execute(sql, params)
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def get_course(conn: duckdb.DuckDBPyConnection, course_id: int) -> dict[str, Any] | None:
    result = conn.execute(
        "SELECT * EXCLUDE (extra) FROM courses WHERE course_id = ?", [course_id]
    )
    row = result.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in result.description], row, strict=True))


def get_course_waypoints(
    conn: duckdb.DuckDBPyConnection, course_id: int
) -> list[dict[str, Any]]:
    result = conn.execute(
        """
        SELECT waypoint_index, name, point_type, distance_m, elevation_m
        FROM course_waypoints WHERE course_id = ? ORDER BY waypoint_index
        """,
        [course_id],
    )
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def count_course_points(conn: duckdb.DuckDBPyConnection, course_id: int) -> int:
    return int(
        _one(
            conn.execute("SELECT count(*) FROM course_points WHERE course_id = ?", [course_id])
        )[0]
    )


def get_course_profile(
    conn: duckdb.DuckDBPyConnection,
    course_id: int,
    *,
    max_points: int = 100,
) -> dict[str, list[Any]]:
    """Down-sampled distance/elevation profile — enough to read the terrain
    shape without spending tokens on a couple thousand raw GPS points.

    Elevation is averaged per bucket like `get_streams` does for activities;
    distance takes the last value in the bucket, since it is cumulative and
    averaging a cumulative counter is meaningless.
    """
    total = count_course_points(conn, course_id)
    if total == 0:
        return {"distance_m": [], "elevation_m": []}

    bucket_size = max(1, -(-total // max_points))
    result = conn.execute(
        """
        SELECT last(distance_m ORDER BY point_index) AS distance_m,
               avg(elevation_m) AS elevation_m
        FROM (
            SELECT *, (row_number() OVER (ORDER BY point_index) - 1) // ? AS bucket
            FROM course_points WHERE course_id = ?
        )
        GROUP BY bucket ORDER BY bucket
        """,
        [bucket_size, course_id],
    )
    columns = [d[0] for d in result.description]
    rows = result.fetchall()
    return {column: [row[i] for row in rows] for i, column in enumerate(columns)}


def course_segment_gain(
    conn: duckdb.DuckDBPyConnection,
    course_id: int,
    from_m: float,
    to_m: float,
) -> float:
    """Elevation gained between two distances along a course's track.

    Computed from consecutive point-to-point deltas at full resolution —
    `get_course_profile`'s down-sampling would average away exactly the
    short, steep climbs a fuelling plan most needs to see.
    """
    row = _one(
        conn.execute(
            """
            WITH deltas AS (
                SELECT distance_m,
                       elevation_m - LAG(elevation_m) OVER (ORDER BY point_index) AS delta
                FROM course_points WHERE course_id = ?
            )
            SELECT coalesce(sum(delta), 0)
            FROM deltas
            WHERE delta > 0 AND distance_m >= ? AND distance_m <= ?
            """,
            [course_id, from_m, to_m],
        )
    )
    return float(row[0])


def sleep_history(
    conn: duckdb.DuckDBPyConnection,
    *,
    since: date | None = None,
    until: date | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Nightly sleep, most recent first."""
    sql = """
        SELECT calendar_date, sleep_start_local, sleep_end_local, total_sleep_s,
               nap_s, deep_sleep_s, light_sleep_s, rem_sleep_s, awake_s,
               avg_hrv, avg_spo2, avg_respiration, sleep_score, sleep_quality,
               hrv_status, resting_heart_rate, body_battery_change
        FROM sleep_sessions WHERE 1 = 1
    """
    params: list[Any] = []
    if since is not None:
        sql += " AND calendar_date >= ?"
        params.append(since)
    if until is not None:
        sql += " AND calendar_date <= ?"
        params.append(until)
    sql += " ORDER BY calendar_date DESC LIMIT ?"
    params.append(limit)

    result = conn.execute(sql, params)
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


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


def activities_with_plan_flag(
    conn: duckdb.DuckDBPyConnection,
    since: date,
    until: date,
) -> list[dict[str, Any]]:
    """Activities in a date range, each saying whether it carries a prescription.

    The flag is what makes calendar adherence possible: an activity recorded on
    a day something was scheduled, but with no `workout_steps`, was not started
    from that workout — the watch never received it, or the athlete ran free.
    Either way the step-by-step comparison is unavailable and the report should
    say so rather than reporting nothing.
    """
    result = conn.execute(
        f"""
        SELECT {_SUMMARY_COLUMNS},
               EXISTS (SELECT 1 FROM workout_steps w
                       WHERE w.activity_id = v.activity_id) AS has_plan
        FROM v_activity_summary v
        WHERE parent_activity_id IS NULL
          AND start_time_local >= ?
          AND start_time_local < ?
        ORDER BY start_time_local
        """,
        [
            datetime.combine(since, datetime.min.time()),
            datetime.combine(until, datetime.max.time()),
        ],
    )
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def strength_sets(conn: duckdb.DuckDBPyConnection, activity_id: int) -> list[dict[str, Any]]:
    """Every set of one strength session, rests included, in the order done."""
    result = conn.execute(
        """
        SELECT set_index, is_rest, start_time_utc, duration_s, repetitions,
               weight_kg, category, exercise_subtype, wkt_step_index
        FROM strength_sets
        WHERE activity_id = ?
        ORDER BY set_index
        """,
        [activity_id],
    )
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def strength_history(
    conn: duckdb.DuckDBPyConnection,
    *,
    exercise: str | None = None,
    since: date | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Working sets across sessions, most recent first.

    Rests are excluded here — this answers "how has the bench moved", and a
    rest is not a data point about that. `strength_sets` keeps them for
    anyone asking about one session in detail.

    Grouped per (session, exercise, load) so a back-off block reads as one
    line rather than four identical ones, which is how the prescription was
    written in the first place.
    """
    sql = """
        SELECT a.activity_id,
               a.start_time_local,
               a.name,
               s.category,
               s.weight_kg,
               count(*)              AS sets,
               min(s.repetitions)    AS min_reps,
               max(s.repetitions)    AS max_reps,
               sum(s.repetitions)    AS total_reps
        FROM strength_sets s
        JOIN activities a USING (activity_id)
        WHERE NOT s.is_rest
    """
    params: list[Any] = []
    if exercise is not None:
        sql += " AND lower(s.category) LIKE ?"
        params.append(f"%{exercise.lower()}%")
    if since is not None:
        sql += " AND a.start_time_local >= ?"
        params.append(datetime.combine(since, datetime.min.time()))
    sql += """
        GROUP BY a.activity_id, a.start_time_local, a.name, s.category, s.weight_kg
        ORDER BY a.start_time_local DESC, s.weight_kg DESC
        LIMIT ?
    """
    params.append(limit)

    result = conn.execute(sql, params)
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


# ══════════════════════════════════════════════════════════════════════
# Location queries: finding activities by where they started, and reading
# back the GPS track that was kept.
#
# These read `records`, the per-sample table the FIT parser fills at full
# resolution — never `get_streams`, which averages into buckets for the
# conversation and would put the athlete off the road.
# ══════════════════════════════════════════════════════════════════════

# What counts as a usable fix. Kept identical to `domain.geo.is_valid_fix` —
# the two are tested against each other — because a search and an export that
# disagree about validity would report a track the export then cannot produce.
_VALID_FIX = (
    "lat IS NOT NULL AND lon IS NOT NULL "
    "AND lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180 "
    "AND NOT (lat = 0 AND lon = 0)"
)

# Whitelisted ORDER BY clauses. The sort name arrives from a model; it selects
# one of these and is never placed in the SQL text itself.
_LOCAL_SORTS: dict[str, str] = {
    "nearest": "start_distance_m ASC, start_time_local DESC, activity_id",
    "recent": "start_time_local DESC, activity_id",
}

# Which stored duration a range applies to. Moving time stops when the watch
# is paused; elapsed time does not. Neither is ever substituted for the other.
_DURATION_COLUMNS: dict[str, str] = {
    "moving": "total_timer_time_s",
    "elapsed": "total_elapsed_time_s",
}


def find_activities_near(
    conn: duckdb.DuckDBPyConnection,
    *,
    lat: float,
    lon: float,
    radius_m: float,
    sport: str | None = "running",
    sub_sport: str | None = None,
    min_distance_m: float | None = None,
    max_distance_m: float | None = None,
    min_ascent_m: float | None = None,
    max_ascent_m: float | None = None,
    min_duration_s: float | None = None,
    max_duration_s: float | None = None,
    duration_basis: str = "moving",
    sort: str = "nearest",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Activities that *started* within `radius_m` of a point, and how many match.

    The radius applies to the start point recorded in the activity summary —
    not to any other point of the track. A run that starts 30 km away and
    passes through the centre of the circle is not found, by design.

    A range bound on a measure the activity does not have (no ascent recorded)
    excludes it: a missing value cannot satisfy "at least 800 m of climb".
    Returns the page and the total before pagination, from one scan.
    """
    from garmin_mcp.domain.geo import EARTH_RADIUS_M, bounding_box

    if sort not in _LOCAL_SORTS:
        raise ValueError(f"unknown sort {sort!r}; available: {sorted(_LOCAL_SORTS)}")
    if duration_basis not in _DURATION_COLUMNS:
        raise ValueError(
            f"unknown duration_basis {duration_basis!r}; available: {sorted(_DURATION_COLUMNS)}"
        )
    duration_column = _DURATION_COLUMNS[duration_basis]

    lat_min, lat_max, lon_min, lon_max = bounding_box(lat, lon, radius_m)

    where = [
        "start_lat IS NOT NULL",
        "start_lon IS NOT NULL",
        "start_lat BETWEEN ? AND ?",
    ]
    params: list[Any] = [lat_min, lat_max]
    if lon_min is not None and lon_max is not None:
        where.append("start_lon BETWEEN ? AND ?")
        params += [lon_min, lon_max]
    if sport is not None:
        where.append("sport = ?")
        params.append(sport)
    if sub_sport is not None:
        where.append("sub_sport = ?")
        params.append(sub_sport)

    for column, low, high in (
        ("total_distance_m", min_distance_m, max_distance_m),
        ("total_ascent_m", min_ascent_m, max_ascent_m),
        (duration_column, min_duration_s, max_duration_s),
    ):
        if low is not None:
            where.append(f"{column} >= ?")
            params.append(low)
        if high is not None:
            where.append(f"{column} <= ?")
            params.append(high)

    candidates = f"""
        WITH candidates AS (
            SELECT activity_id, parent_activity_id, name, sport, sub_sport,
                   start_time_local, start_lat, start_lon,
                   total_distance_m, total_ascent_m, total_descent_m,
                   total_timer_time_s, total_elapsed_time_s,
                   2 * {EARTH_RADIUS_M} * asin(least(1.0, sqrt(
                       pow(sin(radians(start_lat - ?) / 2), 2)
                       + cos(radians(?)) * cos(radians(start_lat))
                         * pow(sin(radians(start_lon - ?) / 2), 2)
                   ))) AS start_distance_m
            FROM activities
            WHERE {" AND ".join(where)}
        )
    """
    # The three leading parameters belong to the distance expression, which
    # comes before the WHERE clause in the statement text.
    bound = [lat, lat, lon, *params, radius_m]

    result = conn.execute(
        candidates
        + f"""
        SELECT *, count(*) OVER () AS total_matches
        FROM candidates
        WHERE start_distance_m <= ?
        ORDER BY {_LOCAL_SORTS[sort]}
        LIMIT ? OFFSET ?
        """,
        [*bound, limit, offset],
    )
    columns = [d[0] for d in result.description]
    rows = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]

    if rows:
        total = int(rows[0]["total_matches"])
    elif offset > 0:
        # A page past the end carries no row to read the total from.
        total = int(
            _one(
                conn.execute(
                    candidates + "SELECT count(*) FROM candidates WHERE start_distance_m <= ?",
                    bound,
                )
            )[0]
        )
    else:
        total = 0
    for row in rows:
        del row["total_matches"]
    return rows, total


def gps_quality(
    conn: duckdb.DuckDBPyConnection,
    activity_ids: list[int],
    gap_threshold_s: float,
) -> dict[int, dict[str, Any]]:
    """Per-activity GPS evidence, computed from the stored samples.

    One scan for the whole page. Returns, for each id: how many samples exist,
    how many carry a valid fix and an altitude, when the first and last fixes
    happened, how many holes longer than the threshold there are, and the
    longest one. An id with no samples at all is simply absent.
    """
    if not activity_ids:
        return {}

    result = conn.execute(
        f"""
        WITH ids AS (SELECT unnest(?::BIGINT[]) AS activity_id),
        totals AS (
            SELECT activity_id, count(*) AS samples, max(elapsed_s) AS last_sample_s
            FROM records WHERE activity_id IN (SELECT activity_id FROM ids)
            GROUP BY activity_id
        ),
        fixes AS (
            SELECT activity_id, elapsed_s, altitude_m,
                   elapsed_s - lag(elapsed_s) OVER (
                       PARTITION BY activity_id ORDER BY ts, elapsed_s
                   ) AS gap_s
            FROM records
            WHERE activity_id IN (SELECT activity_id FROM ids) AND {_VALID_FIX}
        ),
        fix_stats AS (
            SELECT activity_id,
                   count(*)                                  AS gps_points,
                   count(altitude_m)                         AS altitude_points,
                   min(elapsed_s)                            AS first_fix_s,
                   max(elapsed_s)                            AS last_fix_s,
                   count(*) FILTER (WHERE gap_s > ?)         AS interruptions,
                   max(gap_s)                                AS longest_gap_s
            FROM fixes GROUP BY activity_id
        )
        SELECT t.activity_id, t.samples, t.last_sample_s,
               coalesce(f.gps_points, 0)      AS gps_points,
               coalesce(f.altitude_points, 0) AS altitude_points,
               f.first_fix_s, f.last_fix_s,
               coalesce(f.interruptions, 0)   AS interruptions,
               f.longest_gap_s
        FROM totals t LEFT JOIN fix_stats f USING (activity_id)
        """,
        [activity_ids, gap_threshold_s],
    )
    columns = [d[0] for d in result.description]
    return {
        int(row[0]): dict(zip(columns, row, strict=True)) for row in result.fetchall()
    }


def get_route_fixes(
    conn: duckdb.DuckDBPyConnection, activity_id: int, *, limit: int
) -> list[tuple[Any, ...]]:
    """Every valid GPS fix of an activity, in recording order.

    Each row: (ts, elapsed_s, lat, lon, altitude_m, distance_m). Stored values
    only — no interpolation, no smoothing. `limit` is a safety stop, not a
    page: the caller checks the count first and refuses a track that would not
    fit rather than silently truncating it.
    """
    return conn.execute(
        f"""
        SELECT ts, elapsed_s, lat, lon, altitude_m, distance_m
        FROM records
        WHERE activity_id = ? AND {_VALID_FIX}
        ORDER BY ts, elapsed_s
        LIMIT ?
        """,
        [activity_id, limit],
    ).fetchall()


def count_route_fixes(conn: duckdb.DuckDBPyConnection, activity_id: int) -> int:
    return int(
        _one(
            conn.execute(
                f"SELECT count(*) FROM records WHERE activity_id = ? AND {_VALID_FIX}",
                [activity_id],
            )
        )[0]
    )


def get_course_fixes(conn: duckdb.DuckDBPyConnection, course_id: int) -> list[tuple[Any, ...]]:
    """A saved course's stored track: (lat, lon, elevation_m, distance_m)."""
    return conn.execute(
        """
        SELECT lat, lon, elevation_m, distance_m
        FROM course_points WHERE course_id = ? ORDER BY point_index
        """,
        [course_id],
    ).fetchall()


def count_activities_with_gps(conn: duckdb.DuckDBPyConnection) -> int:
    """Activities whose summary carries a start position."""
    return int(
        _one(
            conn.execute(
                "SELECT count(*) FROM activities WHERE start_lat IS NOT NULL "
                "AND start_lon IS NOT NULL"
            )
        )[0]
    )


def count_courses(conn: duckdb.DuckDBPyConnection) -> int:
    return int(_one(conn.execute("SELECT count(*) FROM courses"))[0])


def schema_version(conn: duckdb.DuckDBPyConnection) -> int | None:
    row = _one(conn.execute("SELECT max(version) FROM schema_meta"))
    return int(row[0]) if row[0] is not None else None

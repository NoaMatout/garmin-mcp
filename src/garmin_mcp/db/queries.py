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

from datetime import date, datetime
from typing import Any

import duckdb


def latest_garmin_start(conn: duckdb.DuckDBPyConnection) -> datetime | None:
    """When the newest Garmin-sourced activity began.

    The sync watermark. Manually imported files are excluded on purpose: a
    2019 file dropped into the inbox last week must not make the next sync
    believe it is already up to date.
    """
    row = conn.execute(
        "SELECT max(start_time_utc) FROM activities WHERE source = 'garmin'"
    ).fetchone()
    return row[0] if row and row[0] else None


def known_garmin_ids(conn: duckdb.DuckDBPyConnection) -> set[int]:
    """Every Garmin activity id already stored, parents and legs alike."""
    rows = conn.execute(
        "SELECT DISTINCT garmin_activity_id FROM activities "
        "WHERE garmin_activity_id IS NOT NULL"
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
    activities, first, last = conn.execute(
        """
        SELECT count(*), min(start_time_local), max(start_time_local)
        FROM activities
        """
    ).fetchone()
    records = conn.execute("SELECT count(*) FROM records").fetchone()[0]
    parsed = conn.execute(
        "SELECT count(*) FROM files WHERE status = 'parsed'"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT count(*) FROM files WHERE status = 'failed'"
    ).fetchone()[0]
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

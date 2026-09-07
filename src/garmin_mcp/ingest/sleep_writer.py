"""Writing nightly sleep into DuckDB.

One row per calendar date, upserted rather than deleted-and-reinserted. The
activity writer uses DELETE-then-INSERT because a FIT file fans out into
several tables that must stay consistent with each other; a sleep night is one
self-contained row with a natural single-column key, so `ON CONFLICT ... DO
UPDATE` says the same thing more directly and is exactly what `files` already
does for the same reason (see `ingest/writer.py`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import duckdb

from garmin_mcp.domain.models import SleepSession
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

# Column order duplicated from schema.sql on purpose, same rule as
# ingest/writer.py: an INSERT that relies on table order breaks silently when
# a column is added in the middle.
_COLUMNS = (
    "calendar_date",
    "source",
    "sleep_start_utc",
    "sleep_end_utc",
    "sleep_start_local",
    "sleep_end_local",
    "total_sleep_s",
    "nap_s",
    "deep_sleep_s",
    "light_sleep_s",
    "rem_sleep_s",
    "awake_s",
    "avg_hrv",
    "avg_spo2",
    "avg_respiration",
    "lowest_respiration",
    "highest_respiration",
    "sleep_score",
    "sleep_quality",
    "ingested_at",
    "extra",
)


def _row(session: SleepSession, ingested_at: datetime) -> tuple[Any, ...]:
    return (
        session.calendar_date,
        session.source,
        session.sleep_start_utc,
        session.sleep_end_utc,
        session.sleep_start_local,
        session.sleep_end_local,
        session.total_sleep_s,
        session.nap_s,
        session.deep_sleep_s,
        session.light_sleep_s,
        session.rem_sleep_s,
        session.awake_s,
        session.avg_hrv,
        session.avg_spo2,
        session.avg_respiration,
        session.lowest_respiration,
        session.highest_respiration,
        session.sleep_score,
        session.sleep_quality,
        ingested_at,
        json.dumps(session.extra, default=str) if session.extra else None,
    )


def write_sleep(conn: duckdb.DuckDBPyConnection, session: SleepSession) -> None:
    """Upsert one night. Re-writing the same date replaces it in place."""
    updates = ", ".join(f"{column} = excluded.{column}" for column in _COLUMNS[1:])
    conn.execute(
        f"""
        INSERT INTO sleep_sessions ({", ".join(_COLUMNS)})
        VALUES ({", ".join("?" * len(_COLUMNS))})
        ON CONFLICT (calendar_date) DO UPDATE SET {updates}
        """,
        _row(session, datetime.now(UTC)),
    )
    log.debug("sleep.written", calendar_date=str(session.calendar_date))

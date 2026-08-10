"""DuckDB connection management.

The concurrency constraint that shapes this whole module: **DuckDB allows
either one read-write process, or several read-only processes — never both at
once.** A reader cannot even open the file while the writer holds it.

Two consequences, and they are the reason the ingest worker and the MCP server
are separate processes:

1. The writer must hold the connection for as short a time as possible. The
   worker opens read-write, flushes one activity, and closes — it does not keep
   a connection alive between sync cycles.
2. The reader must expect transient failure. `reading()` retries with
   exponential backoff instead of surfacing a lock error to the MCP client,
   because a sync in progress is a normal state, not an error.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.errors import DatabaseLockedError
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

# Retry budget for readers. Five attempts with a 0.15 s base doubles up to
# ~2.3 s total, which covers a normal write burst without making a broken
# setup feel like a hang.
_READ_RETRIES = 5
_READ_BASE_DELAY = 0.15


def _is_lock_error(exc: Exception) -> bool:
    """Distinguish 'someone else holds the file' from a real IO failure."""
    text = str(exc).lower()
    return "lock" in text or "being used by another process" in text


@contextmanager
def writing(settings: Settings | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open the database read-write. Keep the block short.

    Every second spent inside this context is a second during which the MCP
    server cannot read.
    """
    settings = settings or get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(settings.db_path), read_only=False)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def reading(
    settings: Settings | None = None,
    *,
    retries: int = _READ_RETRIES,
    base_delay: float = _READ_BASE_DELAY,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open the database read-only, retrying while the writer holds the lock.

    Raises DatabaseLockedError once the retry budget is exhausted, so the MCP
    tool layer can turn it into an actionable message rather than a stack trace.
    """
    settings = settings or get_settings()

    if not settings.db_path.exists():
        # A read-only connection cannot create the file, and the resulting
        # DuckDB error is unhelpful. Fail with something the user can act on.
        raise DatabaseLockedError(str(settings.db_path)) from FileNotFoundError(
            f"{settings.db_path} does not exist — run `garmin-mcp init-db` first"
        )

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            conn = duckdb.connect(str(settings.db_path), read_only=True)
        except duckdb.IOException as exc:
            if not _is_lock_error(exc):
                raise
            last_exc = exc
            delay = base_delay * (2**attempt)
            log.debug(
                "db.read_locked", attempt=attempt + 1, retries=retries, retry_in_s=round(delay, 3)
            )
            time.sleep(delay)
            continue

        try:
            yield conn
        finally:
            conn.close()
        return

    log.warning("db.read_lock_timeout", path=str(settings.db_path), attempts=retries)
    raise DatabaseLockedError(str(settings.db_path)) from last_exc


def connect_memory() -> duckdb.DuckDBPyConnection:
    """In-memory database, used by the test suite."""
    return duckdb.connect(":memory:")


def database_exists(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return settings.db_path.exists()


def resolve_db_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.db_path

"""Schema creation and migration.

Deliberately minimal: `schema.sql` is written with `CREATE TABLE IF NOT EXISTS`
throughout, so applying it to an existing database is a no-op. Anything that
cannot be expressed that way — dropping a column, backfilling — goes in
`_MIGRATIONS` as an explicit numbered step.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.db.connection import writing
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 2

# Bumping this forces every stored FIT file to be re-parsed on the next sync,
# without re-downloading. Raise it whenever fit_parser starts extracting a
# field it previously ignored.
PARSER_VERSION = 2

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# Ordered post-baseline steps: {target_version: callable}. Empty at v1.
def _v2_add_workout_step_link(conn: duckdb.DuckDBPyConnection) -> None:
    """Link laps to the prescribed step they belong to.

    The table itself is created by the baseline DDL; only this column needs an
    ALTER, since `laps` predates it.
    """
    conn.execute("ALTER TABLE laps ADD COLUMN IF NOT EXISTS wkt_step_index INTEGER")


_MIGRATIONS: dict[int, Callable[[duckdb.DuckDBPyConnection], None]] = {
    2: _v2_add_workout_step_link,
}


def _current_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Highest applied schema version, or 0 on a fresh database."""
    tables = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_meta'"
    ).fetchone()
    if tables is None:
        return 0
    row = conn.execute("SELECT max(version) FROM schema_meta").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _stamp(conn: duckdb.DuckDBPyConnection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_meta (version, applied_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [version, datetime.now(UTC)],
    )


def apply_schema(conn: duckdb.DuckDBPyConnection) -> int:
    """Bring `conn` up to SCHEMA_VERSION. Returns the resulting version.

    Safe to call on every startup: the baseline DDL is idempotent and each
    numbered migration runs at most once.
    """
    before = _current_version(conn)

    conn.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _stamp(conn, 1)

    for version in sorted(_MIGRATIONS):
        if version > max(before, 1):
            log.info("db.migrating", to_version=version)
            _MIGRATIONS[version](conn)
            _stamp(conn, version)

    after = _current_version(conn)
    if after != before:
        log.info("db.schema_applied", from_version=before, to_version=after)
    return after


def init_database(settings: Settings | None = None) -> int:
    """Create the database file if needed and apply the schema."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    with writing(settings) as conn:
        version = apply_schema(conn)
    log.info("db.ready", path=str(settings.db_path), schema_version=version)
    return version

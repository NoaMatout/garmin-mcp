"""Command-line interface.

Deliberately separate from the MCP server. Anything interactive — logging in,
answering an MFA prompt — belongs here, where a human is present. The server
never authenticates and never blocks on input; it reads a database that this
CLI and the ingest worker fill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from garmin_mcp.config import Backend, get_settings
from garmin_mcp.db.connection import reading
from garmin_mcp.db.migrations import SCHEMA_VERSION, init_database
from garmin_mcp.errors import GarminMcpError
from garmin_mcp.logging import configure_logging, get_logger

app = typer.Typer(
    name="garmin-mcp",
    help="Expose Garmin Connect activity data to Claude over MCP.",
    no_args_is_help=True,
    add_completion=False,
)
log = get_logger(__name__)


@app.callback()
def main() -> None:
    """Configure logging once, before any subcommand runs."""
    configure_logging()


@app.command("init-db")
def init_db() -> None:
    """Create the DuckDB database and apply the schema."""
    settings = get_settings()
    version = init_database(settings)
    typer.echo(f"database ready at {settings.db_path} (schema v{version})")


@app.command("import")
def import_files(
    path: Annotated[
        Path | None,
        typer.Argument(help="A single FIT file to import. Defaults to the whole inbox."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-ingest even if already known.")
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Leave files in the inbox instead of moving them.")
    ] = False,
) -> None:
    """Import FIT files from disk — the degraded mode that needs no Garmin auth.

    Drop files into data/inbox/ and run this. Works offline, with no
    credentials, which is what makes it the ingestion path that keeps working
    when Garmin changes its authentication again.
    """
    from garmin_mcp.db.connection import writing
    from garmin_mcp.ingest.pipeline import IngestReport, import_inbox, ingest_path

    settings = get_settings()
    settings.ensure_dirs()
    init_database(settings)

    try:
        if path is not None:
            if not path.is_file():
                raise typer.BadParameter(f"not a file: {path}")
            report = IngestReport()
            with writing(settings) as conn:
                report.outcomes.append(
                    ingest_path(conn, path, settings=settings, force=force)
                )
        else:
            report = import_inbox(settings, force=force, keep_originals=keep)
    except GarminMcpError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    if not report.outcomes:
        typer.echo(f"nothing to import — drop FIT files into {settings.inbox_dir}")
        return

    for outcome in report.outcomes:
        colour = {
            "imported": typer.colors.GREEN,
            "replaced": typer.colors.YELLOW,
            "skipped": typer.colors.BLUE,
            "failed": typer.colors.RED,
        }[outcome.status]
        detail = f" — {outcome.reason}" if outcome.reason else (
            f" — {outcome.activities} activities, {outcome.records} samples"
        )
        typer.secho(f"  {outcome.status:<9} {outcome.name}{detail}", fg=colour)

    typer.echo(report.summary())
    if report.failed:
        raise typer.Exit(1)


@app.command()
def auth(
    backend: Annotated[
        Backend | None,
        typer.Option("--backend", help="Override GARMIN_BACKEND for this login."),
    ] = None,
    email: Annotated[
        str | None, typer.Option("--email", help="Overrides GARMIN_EMAIL.")
    ] = None,
) -> None:
    """Log in to Garmin and save the session.

    The only command that ever handles a password, and the only one that can
    prompt for an MFA code. Everything else — the sync, the worker, the MCP
    server — reuses the token this leaves behind, which is what lets them run
    unattended.

    The password is read from the environment, never written to disk and never
    logged; only the resulting OAuth token is persisted, owner-readable.
    """
    from garmin_mcp.garmin import auth as auth_module

    settings = get_settings()
    settings.ensure_dirs()
    chosen = backend or settings.backend

    password = None
    if chosen is not Backend.PLAYWRIGHT:
        password = (
            settings.password.get_secret_value() if settings.password else None
        ) or typer.prompt("Garmin password", hide_input=True)

    try:
        profile = auth_module.login(
            chosen,
            settings,
            email=email,
            password=password,
            mfa_prompt=lambda: typer.prompt("MFA code"),
        )
    except GarminMcpError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        password = None

    who = f" as {profile}" if profile else ""
    typer.secho(f"signed in{who} — session saved to {settings.token_dir}", fg=typer.colors.GREEN)


@app.command()
def sync(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Maximum activities to pull.")
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="Start date, YYYY-MM-DD.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-download and re-ingest known activities.")
    ] = False,
) -> None:
    """Download new activities from Garmin and ingest them.

    Incremental: only what is newer than the most recent Garmin activity
    already stored. If Garmin is unreachable, the message says how to keep
    going through the inbox instead.
    """
    from datetime import date as date_type

    from garmin_mcp.garmin.factory import resolve_source
    from garmin_mcp.ingest.pipeline import sync_from_garmin

    settings = get_settings()
    settings.ensure_dirs()
    init_database(settings)

    start = None
    if since:
        try:
            start = date_type.fromisoformat(since)
        except ValueError as exc:
            raise typer.BadParameter("--since must look like 2026-03-15") from exc

    try:
        source, status = resolve_source(settings)
    except GarminMcpError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"using {status.describe()}")
    try:
        report = sync_from_garmin(source, settings, limit=limit, since=start, force=force)
    except GarminMcpError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        source.close()

    if not report.outcomes:
        typer.secho("already up to date", fg=typer.colors.GREEN)
        return
    for outcome in report.outcomes:
        typer.echo(f"  {outcome.status:<9} {outcome.name}")
    typer.echo(report.summary())


@app.command()
def status() -> None:
    """Report whether Garmin is currently reachable.

    Never raises: an unusable backend is the answer, not a crash.
    """
    from garmin_mcp.garmin.factory import build_source

    settings = get_settings()
    for backend in (Backend.CFFI, Backend.PLAYWRIGHT):
        source = build_source(backend, settings)
        try:
            result = source.health_check()
        finally:
            source.close()
        colour = typer.colors.GREEN if result.usable else typer.colors.YELLOW
        typer.secho(f"  {result.describe()}", fg=colour)

    typer.echo()
    typer.echo("manual import always works: drop FIT files in "
               f"{settings.inbox_dir} and run `garmin-mcp import`")


@app.command()
def info() -> None:
    """Show what is currently in the database."""
    settings = get_settings()
    if not settings.db_path.exists():
        typer.echo(f"no database at {settings.db_path} — run `garmin-mcp init-db`")
        raise typer.Exit(1)

    with reading(settings) as conn:
        activities, first, last = conn.execute(
            "SELECT count(*), min(start_time_local), max(start_time_local) FROM activities"
        ).fetchone()
        records = conn.execute("SELECT count(*) FROM records").fetchone()[0]
        files = conn.execute("SELECT count(*) FROM files WHERE status = 'parsed'").fetchone()[0]
        failed = conn.execute("SELECT count(*) FROM files WHERE status = 'failed'").fetchone()[0]
        by_sport = conn.execute(
            """
            SELECT sport, count(*) AS n, sum(total_distance_m) / 1000 AS km
            FROM activities
            WHERE parent_activity_id IS NULL
            GROUP BY sport ORDER BY n DESC
            """
        ).fetchall()

    typer.echo(f"database   {settings.db_path} (schema v{SCHEMA_VERSION})")
    typer.echo(f"files      {files} parsed, {failed} failed")
    typer.echo(f"activities {activities}")
    typer.echo(f"samples    {records:,}")
    if first and last:
        typer.echo(f"range      {first:%Y-%m-%d} → {last:%Y-%m-%d}")
    for sport, count, km in by_sport:
        typer.echo(f"  {sport or 'unknown':<16} {count:>4} activities  {km or 0:>9,.1f} km")


if __name__ == "__main__":
    app()

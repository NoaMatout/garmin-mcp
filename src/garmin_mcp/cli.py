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
from garmin_mcp.db import queries
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
                report.outcomes.append(ingest_path(conn, path, settings=settings, force=force))
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
        detail = (
            f" — {outcome.reason}"
            if outcome.reason
            else (f" — {outcome.activities} activities, {outcome.records} samples")
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
    email: Annotated[str | None, typer.Option("--email", help="Overrides GARMIN_EMAIL.")] = None,
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
    since: Annotated[str | None, typer.Option("--since", help="Start date, YYYY-MM-DD.")] = None,
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
    typer.echo(
        "manual import always works: drop FIT files in "
        f"{settings.inbox_dir} and run `garmin-mcp import`"
    )


@app.command()
def serve(
    transport: Annotated[
        str | None,
        typer.Option("--transport", help="stdio or streamable-http. Overrides the env."),
    ] = None,
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
) -> None:
    """Run the MCP server.

    Defaults to stdio, which is what a desktop MCP client launches. Nothing is
    printed to stdout: it carries the JSON-RPC frames, and a single stray line
    corrupts the stream.
    """
    from garmin_mcp.config import Transport
    from garmin_mcp.server.transports import run_http, run_stdio

    settings = get_settings()
    chosen = Transport(transport) if transport else settings.mcp_transport

    if not settings.db_path.exists():
        # Warn rather than refuse: the tools report this cleanly, and a client
        # that cannot start its server is harder to diagnose than empty results.
        log.warning("server.no_database", path=str(settings.db_path))

    if chosen is Transport.HTTP:
        run_http(host or settings.mcp_host, port or settings.mcp_port)
    else:
        run_stdio()


ENV_TEMPLATE = """\
# Written by `garmin-mcp setup`. Gitignored — keep it that way.
# Every other setting has a working default; see .env.example to tune them.

GARMIN_EMAIL={email}
GARMIN_PASSWORD={password}
GARMIN_BACKEND={backend}
"""


@app.command()
def setup() -> None:
    """Interactive first-run setup: credentials, database, and a check that it works.

    Only two values are actually required — everything else in .env.example is
    optional tuning. This asks for those two, writes an owner-readable .env,
    and then proves the whole chain works rather than leaving you to find out
    later.

    The password is typed with echo off, goes straight into .env on this
    machine, and is never logged, transmitted, or stored in the database.
    """
    settings = get_settings()
    env_path = Path(".env")

    typer.secho("\ngarmin-mcp setup", bold=True)
    typer.echo("Two values are required. Everything else has a sensible default.\n")

    if env_path.exists():
        typer.secho(f"{env_path} already exists.", fg=typer.colors.YELLOW)
        if not typer.confirm("Overwrite it?", default=False):
            typer.echo("Keeping the existing file. Skipping to the connection test.")
            _verify_setup(settings)
            return

    typer.echo("How should the project talk to Garmin?")
    typer.echo("  1. cffi       — lightweight HTTP client (recommended, try this first)")
    typer.echo("  2. playwright — real browser, heavier, for when Garmin blocks the above")
    typer.echo("  3. manual     — no Garmin account; import FIT files by hand")
    choice = typer.prompt("Choice", default="1")

    if choice.strip() == "3":
        _write_env(env_path, email="", password="", backend="cffi")
        typer.secho(f"\nWrote {env_path} with no credentials.", fg=typer.colors.GREEN)
        typer.echo(f"Drop FIT files into {settings.inbox_dir} and run `garmin-mcp import`.")
        init_database(settings)
        return

    backend = "playwright" if choice.strip() == "2" else "auto"

    email = typer.prompt("Garmin Connect email")
    password = ""
    if backend != "playwright":
        typer.echo("Password (not shown as you type, saved only to .env on this machine)")
        password = typer.prompt("Garmin password", hide_input=True)

    _write_env(env_path, email=email, password=password, backend=backend)
    typer.secho(f"\nWrote {env_path} (permissions 600)", fg=typer.colors.GREEN)

    # Reload so the new values take effect in this process.
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    init_database(settings)

    if not typer.confirm("\nLog in to Garmin now?", default=True):
        typer.echo("Run `garmin-mcp auth` when you are ready.")
        return

    from garmin_mcp.garmin import auth as auth_module

    try:
        profile = auth_module.login(
            settings.backend,
            settings,
            mfa_prompt=lambda: typer.prompt("MFA code"),
        )
    except GarminMcpError as exc:
        typer.secho(f"\nLogin failed: {exc}", fg=typer.colors.RED, err=True)
        typer.echo(
            "\nThis does not block you: drop FIT files into "
            f"{settings.inbox_dir} and run `garmin-mcp import`."
        )
        raise typer.Exit(1) from exc

    typer.secho(f"Signed in{f' as {profile}' if profile else ''}.", fg=typer.colors.GREEN)

    if typer.confirm("Pull your 5 most recent activities as a test?", default=True):
        from garmin_mcp.garmin.factory import resolve_source
        from garmin_mcp.ingest.pipeline import sync_from_garmin

        source, _ = resolve_source(settings)
        try:
            report = sync_from_garmin(source, settings, limit=5)
        finally:
            source.close()
        typer.echo(report.summary())

    _verify_setup(settings)


def _write_env(path: Path, *, email: str, password: str, backend: str) -> None:
    """Write .env and restrict it to the owner before anything else can read it."""
    path.write_text(
        ENV_TEMPLATE.format(email=email, password=password, backend=backend),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _verify_setup(_settings: object = None) -> None:
    """Confirm the MCP server actually answers, and say how to connect it."""
    from garmin_mcp.server import tools

    get_settings.cache_clear()
    status = tools.database_status()
    if status.get("available"):
        typer.secho(
            f"\nDatabase ready: {status['activities']} activities, {status['samples']:,} samples.",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(f"\nDatabase not ready: {status.get('reason')}", fg=typer.colors.YELLOW)

    typer.echo("\nConnect it to Claude Code with:")
    typer.secho(
        f'  claude mcp add garmin -- uv run --directory "{Path.cwd()}" garmin-mcp serve',
        fg=typer.colors.CYAN,
    )


@app.command()
def worker() -> None:
    """Run the ingest worker: periodic sync, and on-demand runs from Claude.

    The only process that writes to the database. DuckDB grants exclusive
    access to a single writer, so keeping writes in one place is what lets the
    MCP server read without contention.

    Syncs once at startup, then every GARMIN_SYNC_INTERVAL_MINUTES, and picks
    up `sync_now` requests within a couple of seconds. Stops cleanly on SIGTERM
    so `docker stop` does not interrupt a write.
    """
    from garmin_mcp.ingest.worker import run_worker

    settings = get_settings()
    typer.echo(
        f"worker starting — syncing every {settings.sync_interval_minutes} min, "
        f"watching {settings.trigger_dir}",
        err=True,
    )
    run_worker(settings)


@app.command()
def info() -> None:
    """Show what is currently in the database."""
    settings = get_settings()
    if not settings.db_path.exists():
        typer.echo(f"no database at {settings.db_path} — run `garmin-mcp init-db`")
        raise typer.Exit(1)

    with reading(settings) as conn:
        counts = queries.activity_counts(conn)
        activities = counts["activities"]
        records = counts["records"]
        files = counts["files_parsed"]
        failed = counts["files_failed"]
        first, last = counts["first_activity"], counts["last_activity"]
        by_sport = conn.execute(
            """
            SELECT sport, count(*) AS n, sum(total_distance_m) / 1000 AS km
            FROM activities
            WHERE parent_activity_id IS NULL
            GROUP BY sport ORDER BY n DESC
            """
        ).fetchall()

    from garmin_mcp.ingest.worker import read_worker_status

    worker_status = read_worker_status(settings)
    typer.echo(f"database   {settings.db_path} (schema v{SCHEMA_VERSION})")
    if worker_status.alive:
        typer.secho(f"worker     running (pid {worker_status.pid})", fg=typer.colors.GREEN)
    else:
        typer.secho(f"worker     not running — {worker_status.detail}", fg=typer.colors.YELLOW)
    typer.echo(f"files      {files} parsed, {failed} failed")
    typer.echo(f"activities {activities}")
    typer.echo(f"samples    {records:,}")
    if first and last:
        typer.echo(f"range      {first:%Y-%m-%d} → {last:%Y-%m-%d}")
    for sport, count, km in by_sport:
        typer.echo(f"  {sport or 'unknown':<16} {count:>4} activities  {km or 0:>9,.1f} km")


if __name__ == "__main__":
    app()

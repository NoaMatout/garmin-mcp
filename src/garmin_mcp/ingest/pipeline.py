"""Ingestion orchestration.

Two paths lead into the database and they share everything downstream of
"here are some FIT bytes":

* the **Garmin sync**, which downloads what is missing (added in a later
  module, behind the `ActivitySource` interface);
* the **manual inbox**, where dropping files into `data/inbox/` imports them.

The inbox is not a fallback bolted on for completeness. Garmin broke every
third-party client in March 2026 by fingerprinting TLS handshakes, and will do
something like it again. The inbox path touches no network and no credentials,
which makes it the only ingestion route that can be promised to still work in
a year — so it is a first-class citizen, tested as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.db import queries
from garmin_mcp.db.connection import writing
from garmin_mcp.db.migrations import PARSER_VERSION
from garmin_mcp.domain.models import Source
from garmin_mcp.errors import FitParseError, GarminError, GarminRateLimitError, IngestError
from garmin_mcp.garmin.source import ActivitySource
from garmin_mcp.ingest import store
from garmin_mcp.ingest.fit_parser import hash_fit_bytes, parse_fit
from garmin_mcp.ingest.writer import file_is_ingested, record_file_failure, write_parsed
from garmin_mcp.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class FileOutcome:
    name: str
    status: str  # 'imported' | 'replaced' | 'skipped' | 'failed'
    activities: int = 0
    records: int = 0
    reason: str | None = None


@dataclass(slots=True)
class IngestReport:
    outcomes: list[FileOutcome] = field(default_factory=list)

    @property
    def imported(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("imported", "replaced"))

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")

    @property
    def activities(self) -> int:
        return sum(o.activities for o in self.outcomes)

    @property
    def records(self) -> int:
        return sum(o.records for o in self.outcomes)

    def summary(self) -> str:
        return (
            f"{self.imported} imported, {self.skipped} already known, "
            f"{self.failed} failed — {self.activities} activities, "
            f"{self.records} samples"
        )


def ingest_bytes(
    conn: duckdb.DuckDBPyConnection,
    data: bytes,
    *,
    origin: str,
    source: Source = "manual",
    garmin_activity_id: int | None = None,
    activity_name: str | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> FileOutcome:
    """Ingest one file's bytes: dedupe, store, parse, write.

    The hash is computed before anything is written, so a file already in the
    database costs one hash and no parse — which is what keeps re-running a
    sync cheap.
    """
    settings = settings or get_settings()

    try:
        fit_bytes = store.extract_fit_bytes(data)
    except FitParseError as exc:
        return FileOutcome(origin, "failed", reason=str(exc))

    file_hash = hash_fit_bytes(fit_bytes)

    if not force and file_is_ingested(conn, file_hash, PARSER_VERSION):
        log.debug("ingest.already_known", origin=origin, file_hash=file_hash[:12])
        return FileOutcome(origin, "skipped", reason="already ingested")

    # Parse before choosing a filename: the start date decides the shard, and
    # a manual file's identity is derived from its contents.
    temporary = settings.data_dir / ".incoming"
    temporary.mkdir(parents=True, exist_ok=True)
    staged = temporary / f"{file_hash[:16]}.fit"
    staged.write_bytes(fit_bytes)

    try:
        parsed = parse_fit(
            staged,
            file_hash=file_hash,
            source=source,
            garmin_activity_id=garmin_activity_id,
            activity_name=activity_name,
        )
    except FitParseError as exc:
        record_file_failure(
            conn,
            file_hash=file_hash,
            path=origin,
            source=source,
            parser_version=PARSER_VERSION,
            error=str(exc),
            size_bytes=len(fit_bytes),
        )
        staged.unlink(missing_ok=True)
        log.warning("ingest.parse_failed", origin=origin, error=str(exc))
        return FileOutcome(origin, "failed", reason=str(exc))

    primary = parsed.parent or parsed.activities[0]
    stored = store.store_fit_bytes(
        fit_bytes,
        activity_id=primary.activity_id,
        started_at=primary.start_time_utc,
        settings=settings,
    )
    staged.unlink(missing_ok=True)
    parsed.path = str(stored.path)

    try:
        result = write_parsed(
            conn,
            parsed,
            parser_version=PARSER_VERSION,
            size_bytes=stored.bytes_written,
            downloaded_at=datetime.now(UTC),
        )
    except IngestError as exc:
        return FileOutcome(origin, "failed", reason=str(exc))

    return FileOutcome(
        origin,
        "replaced" if result.replaced else "imported",
        activities=result.activities_written,
        records=result.records_written,
    )


def ingest_path(
    conn: duckdb.DuckDBPyConnection,
    path: Path,
    *,
    source: Source = "manual",
    settings: Settings | None = None,
    force: bool = False,
) -> FileOutcome:
    """Ingest a single file from disk."""
    return ingest_bytes(
        conn,
        path.read_bytes(),
        origin=path.name,
        source=source,
        settings=settings,
        force=force,
    )


def import_inbox(
    settings: Settings | None = None,
    *,
    force: bool = False,
    keep_originals: bool = False,
) -> IngestReport:
    """Import every FIT file waiting in `data/inbox/`.

    Files are moved to `_processed/` or `_failed/` afterwards, so the inbox
    always shows what still needs attention and a second run does no work.
    This is the degraded mode: no network, no credentials, no Garmin.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    pending = store.iter_inbox(settings)
    report = IngestReport()
    if not pending:
        log.info("inbox.empty", path=str(settings.inbox_dir))
        return report

    log.info("inbox.scanning", files=len(pending))
    with writing(settings) as conn:
        for path in pending:
            outcome = ingest_path(conn, path, source="manual", settings=settings, force=force)
            report.outcomes.append(outcome)

            if keep_originals:
                continue
            target = (
                settings.inbox_failed_dir
                if outcome.status == "failed"
                else settings.inbox_processed_dir
            )
            store.move_to(path, target)

    log.info("inbox.done", summary=report.summary())
    return report


def sync_from_garmin(
    source: ActivitySource,
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    since: date | None = None,
    force: bool = False,
) -> IngestReport:
    """Download and ingest whatever Garmin has that we do not.

    Incremental by default: the watermark is the newest Garmin-sourced activity
    already stored, so a routine sync lists a handful of entries and downloads
    only what is new. Manually imported files are excluded from that watermark
    — a 2019 file dropped into the inbox last week must not convince the sync
    that it is up to date.

    Activities are processed oldest first. If the run dies halfway, everything
    already written stays written and the next sync resumes from there rather
    than starting over.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    limit = limit or settings.sync_batch_size
    report = IngestReport()

    with writing(settings) as conn:
        watermark = since or _watermark(conn)
        known = queries.known_garmin_ids(conn)

    log.info("sync.listing", since=str(watermark), limit=limit, backend=source.name)
    stubs = source.list_activities(since=watermark, limit=limit)

    pending = [s for s in stubs if force or s.activity_id not in known]
    if not pending:
        log.info("sync.up_to_date", listed=len(stubs))
        return report

    pending.sort(key=lambda s: s.start_time_utc)
    log.info("sync.downloading", count=len(pending))

    for stub in pending:
        label = f"{stub.activity_id} ({stub.sport or 'unknown'})"
        try:
            payload = source.download_fit(stub.activity_id)
        except GarminRateLimitError:
            # Stop the whole run rather than hammer a service already saying no.
            log.warning("sync.rate_limited_stopping", completed=len(report.outcomes))
            report.outcomes.append(FileOutcome(label, "failed", reason="rate limited"))
            break
        except GarminError as exc:
            log.warning("sync.download_failed", activity_id=stub.activity_id, error=str(exc))
            report.outcomes.append(FileOutcome(label, "failed", reason=str(exc)))
            continue

        # One short write window per activity: the MCP server cannot read
        # while this is held, so it is opened and closed per file.
        with writing(settings) as conn:
            report.outcomes.append(
                ingest_bytes(
                    conn,
                    payload,
                    origin=label,
                    source="garmin",
                    garmin_activity_id=stub.activity_id,
                    activity_name=stub.name,
                    settings=settings,
                    force=force,
                )
            )

    log.info("sync.done", summary=report.summary())
    return report


def _watermark(conn: duckdb.DuckDBPyConnection) -> date | None:
    """Where to resume from: the day of the newest Garmin activity we hold.

    Deliberately the whole day rather than the exact instant — Garmin reports
    start times with a granularity that makes an exact cursor prone to skipping
    an activity recorded moments later.
    """
    latest = queries.latest_garmin_start(conn)
    return latest.date() if latest else None

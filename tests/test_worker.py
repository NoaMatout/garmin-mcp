"""Tests for the ingest worker and the sync_now round trip.

The worker exists because DuckDB allows one writer and no concurrent readers.
Everything here is about that coordination holding up: the heartbeat telling
the truth, requests being answered, and — most importantly — a broken Garmin
session degrading into a warning rather than taking the worker down or
blocking the inbox.

No network. A fake source stands in for Garmin, and the request/response cycle
is driven manually rather than by starting a real loop, so the tests are
deterministic instead of sleep-based.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from garmin_mcp.config import Settings
from garmin_mcp.db.migrations import init_database
from garmin_mcp.errors import GarminAuthError, WorkerUnavailableError
from garmin_mcp.ingest import worker as worker_module
from garmin_mcp.ingest.worker import (
    HEARTBEAT_FILENAME,
    REQUEST_SUFFIX,
    RESULT_SUFFIX,
    IngestWorker,
    read_worker_status,
    request_sync,
)
from tests import fit_builder


@pytest.fixture
def db(settings: Settings) -> Settings:
    init_database(settings)
    return settings


@pytest.fixture
def offline_worker(db: Settings, monkeypatch: pytest.MonkeyPatch) -> IngestWorker:
    """A worker whose Garmin side always reports "you are not logged in"."""

    def no_session(*_args: object, **_kwargs: object) -> None:
        raise GarminAuthError("no saved Garmin session")

    monkeypatch.setattr(IngestWorker, "_sync_garmin", no_session)
    return IngestWorker(db)


class TestHeartbeat:
    def test_absent_worker_reports_not_alive(self, db: Settings) -> None:
        status = read_worker_status(db)
        assert not status.alive
        assert "no worker heartbeat" in (status.detail or "")

    def test_a_fresh_heartbeat_reports_alive(self, db: Settings) -> None:
        IngestWorker(db)._heartbeat()
        status = read_worker_status(db)
        assert status.alive
        assert status.pid is not None
        assert status.age_seconds is not None and status.age_seconds < 5

    def test_a_stale_heartbeat_is_not_alive(self, db: Settings) -> None:
        """A crashed worker must not look healthy forever."""
        stale = datetime.now(UTC) - timedelta(seconds=600)
        (db.trigger_dir / HEARTBEAT_FILENAME).write_text(
            json.dumps({"pid": 1, "updated_at": stale.isoformat()})
        )
        status = read_worker_status(db)
        assert not status.alive
        assert "old" in (status.detail or "")

    def test_a_clean_stop_removes_the_heartbeat(self, db: Settings) -> None:
        # A stopped worker should report as stopped at once, not after the
        # staleness window.
        instance = IngestWorker(db)
        instance._heartbeat()
        assert read_worker_status(db).alive
        instance._clear_heartbeat()
        assert not read_worker_status(db).alive

    def test_corrupt_heartbeat_is_not_mistaken_for_alive(self, db: Settings) -> None:
        (db.trigger_dir / HEARTBEAT_FILENAME).write_text("{ this is not json")
        assert not read_worker_status(db).alive


class TestSyncNowRoundTrip:
    def test_requesting_without_a_worker_fails_immediately(self, db: Settings) -> None:
        """Better a fast, actionable error than a two-minute silence."""
        with pytest.raises(WorkerUnavailableError) as caught:
            request_sync(db, timeout_s=30)
        assert "worker" in str(caught.value).lower()
        assert "docker compose" in (caught.value.hint or "")

    def test_the_worker_answers_a_request(self, offline_worker: IngestWorker) -> None:
        db = offline_worker.settings
        (db.trigger_dir / f"sync-abc123{REQUEST_SUFFIX}").write_text(
            json.dumps({"requested_at": datetime.now(UTC).isoformat(), "limit": 5})
        )

        offline_worker._serve_requests()

        result_path = db.trigger_dir / f"sync-abc123{RESULT_SUFFIX}"
        assert result_path.exists()
        assert "finished_at" in json.loads(result_path.read_text())

    def test_the_request_is_consumed_so_it_cannot_run_twice(
        self, offline_worker: IngestWorker
    ) -> None:
        db = offline_worker.settings
        request = db.trigger_dir / f"sync-dup{REQUEST_SUFFIX}"
        request.write_text(json.dumps({"limit": 1}))

        offline_worker._serve_requests()
        assert not request.exists()

    def test_a_failed_garmin_sync_is_reported_not_swallowed(
        self, offline_worker: IngestWorker
    ) -> None:
        """An empty result and a broken session must not look identical."""
        result = offline_worker._run_cycle(limit=None)
        assert "garmin_error" in result
        assert result["garmin_hint"] == "run `garmin-mcp auth`"

    def test_abandoned_requests_expire(self, db: Settings) -> None:
        # A request written while the worker was down must not fire days later.
        import os
        import time

        stale = db.trigger_dir / f"sync-old{REQUEST_SUFFIX}"
        stale.write_text("{}")
        old = time.time() - 3600
        os.utime(stale, (old, old))

        IngestWorker(db)._expire_stale_requests()
        assert not stale.exists()


class TestDegradedMode:
    def test_the_inbox_is_imported_even_when_garmin_is_down(
        self, offline_worker: IngestWorker
    ) -> None:
        """The claim the whole degraded-mode design rests on.

        Garmin being unreachable must not stop files already on disk from
        being ingested — that is the entire reason the inbox path exists.
        """
        db = offline_worker.settings
        (db.inbox_dir / "run.fit").write_bytes(fit_builder.build_run())

        result = offline_worker._run_cycle(limit=None)

        assert result["inbox"]["imported"] == 1
        assert "garmin_error" in result  # Garmin did fail
        with duckdb.connect(str(db.db_path), read_only=True) as conn:
            assert conn.execute("SELECT count(*) FROM activities").fetchone()[0] == 1

    def test_a_corrupt_inbox_file_does_not_stop_the_cycle(
        self, offline_worker: IngestWorker
    ) -> None:
        db = offline_worker.settings
        (db.inbox_dir / "broken.fit").write_bytes(fit_builder.build_corrupt())
        (db.inbox_dir / "good.fit").write_bytes(fit_builder.build_run())

        result = offline_worker._run_cycle(limit=None)

        assert result["inbox"]["imported"] == 1
        assert result["inbox"]["failed"] == 1

    def test_an_unexpected_error_does_not_kill_the_worker(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A long-running process must survive a surprise; the alternative is a
        # container that quietly stops syncing.
        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("something unforeseen")

        monkeypatch.setattr(worker_module, "import_inbox", explode)
        monkeypatch.setattr(IngestWorker, "_sync_garmin", explode)

        result = IngestWorker(db)._run_cycle(limit=None)
        assert "inbox_error" in result
        assert "garmin_error" in result


class TestScheduling:
    def test_the_first_cycle_runs_without_waiting(self, db: Settings) -> None:
        # A container that just started should catch up, not idle for 30 min.
        assert IngestWorker(db)._interval_elapsed()

    def test_the_interval_is_respected_afterwards(self, db: Settings) -> None:
        instance = IngestWorker(db)
        instance._last_sync = datetime.now(UTC)
        assert not instance._interval_elapsed()

    def test_the_interval_elapses_eventually(self, db: Settings) -> None:
        instance = IngestWorker(db)
        instance._last_sync = datetime.now(UTC) - timedelta(minutes=db.sync_interval_minutes + 1)
        assert instance._interval_elapsed()


class TestToolIntegration:
    def test_database_status_reports_a_missing_worker(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stale database and a stopped worker are otherwise indistinguishable.
        from garmin_mcp.server import tools

        monkeypatch.setattr(tools, "_settings", lambda: db)
        status = tools.database_status()
        assert status["sync_worker"]["running"] is False
        assert "garmin-mcp sync" in status["sync_worker"]["note"]

    def test_database_status_reports_a_live_worker(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        IngestWorker(db)._heartbeat()
        monkeypatch.setattr(tools, "_settings", lambda: db)
        assert tools.database_status()["sync_worker"]["running"] is True

    def test_sync_now_without_a_worker_explains_the_fix(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        monkeypatch.setattr(tools, "_settings", lambda: db)
        with pytest.raises(WorkerUnavailableError):
            tools.sync_now()


class TestVersionMismatch:
    """A worker from an older build must not fail as a silent timeout.

    Found in real use: the container was still running an image built before
    workout support existed. It ignored the request kind it did not know, the
    caller waited out its full timeout, and the reported cause — "the worker
    may be busy" — sent the diagnosis down three wrong paths in a row.
    """

    def test_the_heartbeat_advertises_what_the_worker_handles(self, db: Settings) -> None:
        IngestWorker(db)._heartbeat()
        status = read_worker_status(db)
        assert "sync" in status.supports
        assert "workout" in status.supports

    def test_an_unsupported_kind_fails_immediately(self, db: Settings) -> None:
        import json as _json

        (db.trigger_dir / HEARTBEAT_FILENAME).write_text(
            _json.dumps(
                {
                    "pid": 1,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "supports": ["sync"],
                }
            )
        )
        with pytest.raises(WorkerUnavailableError, match="does not handle"):
            worker_module.request("workout", {}, db, timeout_s=999)

    def test_a_heartbeat_without_the_field_is_treated_as_old(self, db: Settings) -> None:
        # Builds predating the field advertise nothing; absence is the signal.
        import json as _json

        (db.trigger_dir / HEARTBEAT_FILENAME).write_text(
            _json.dumps({"pid": 1, "updated_at": datetime.now(UTC).isoformat()})
        )
        with pytest.raises(WorkerUnavailableError, match="predates"):
            worker_module.request("workout", {}, db, timeout_s=999)

    def test_sync_still_works_against_an_older_worker(self, db: Settings) -> None:
        # Backwards compatibility: only the new kind is refused.
        import json as _json

        (db.trigger_dir / HEARTBEAT_FILENAME).write_text(
            _json.dumps({"pid": 1, "updated_at": datetime.now(UTC).isoformat()})
        )
        with pytest.raises(WorkerUnavailableError, match="did not answer"):
            worker_module.request("sync", {}, db, timeout_s=1)

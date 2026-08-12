"""Ingestion must converge, not accumulate.

The same activity genuinely arrives twice in normal use: pulled from Garmin
today, dropped into the inbox by hand next year when the auth breaks again.
If that duplicated a row, every weekly total would silently drift upward —
the kind of bug nobody notices until they trust the numbers.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from garmin_mcp.config import Settings
from garmin_mcp.db.connection import writing
from garmin_mcp.db.migrations import init_database
from garmin_mcp.ingest.pipeline import import_inbox, ingest_path
from tests import fit_builder


@pytest.fixture
def db(settings: Settings) -> Settings:
    init_database(settings)
    return settings


def _counts(settings: Settings) -> tuple[int, int, int, int]:
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        return (
            conn.execute("SELECT count(*) FROM files").fetchone()[0],
            conn.execute("SELECT count(*) FROM activities").fetchone()[0],
            conn.execute("SELECT count(*) FROM laps").fetchone()[0],
            conn.execute("SELECT count(*) FROM records").fetchone()[0],
        )


def _drop_in_inbox(settings: Settings, name: str, data: bytes) -> Path:
    path = settings.inbox_dir / name
    path.write_bytes(data)
    return path


class TestIdempotency:
    def test_importing_twice_changes_nothing(self, db: Settings) -> None:
        _drop_in_inbox(db, "run.fit", fit_builder.build_run())
        first = import_inbox(db)
        after_first = _counts(db)

        _drop_in_inbox(db, "run.fit", fit_builder.build_run())
        second = import_inbox(db)

        assert first.imported == 1
        assert second.imported == 0
        assert second.skipped == 1
        assert _counts(db) == after_first

    def test_the_same_file_under_a_different_name_is_recognised(self, db: Settings) -> None:
        # Identity is the content hash, not the filename — Garmin and a manual
        # export name the same ride differently.
        data = fit_builder.build_run()
        _drop_in_inbox(db, "morning-run.fit", data)
        import_inbox(db)
        baseline = _counts(db)

        _drop_in_inbox(db, "2026-03-15-activity.fit", data)
        report = import_inbox(db)

        assert report.skipped == 1
        assert _counts(db) == baseline

    def test_forcing_replaces_rather_than_duplicates(self, db: Settings) -> None:
        data = fit_builder.build_run()
        _drop_in_inbox(db, "run.fit", data)
        import_inbox(db)
        baseline = _counts(db)

        _drop_in_inbox(db, "run.fit", data)
        report = import_inbox(db, force=True)

        assert report.imported == 1
        assert report.outcomes[0].status == "replaced"
        assert _counts(db) == baseline

    def test_a_re_parse_never_leaves_orphaned_samples(self, db: Settings) -> None:
        # Every record must belong to an activity that still exists.
        data = fit_builder.build_triathlon()
        _drop_in_inbox(db, "tri.fit", data)
        import_inbox(db)
        _drop_in_inbox(db, "tri.fit", data)
        import_inbox(db, force=True)

        with duckdb.connect(str(db.db_path), read_only=True) as conn:
            orphans = conn.execute(
                """
                SELECT count(*) FROM records r
                LEFT JOIN activities a USING (activity_id)
                WHERE a.activity_id IS NULL
                """
            ).fetchone()[0]
        assert orphans == 0

    def test_distinct_activities_accumulate_normally(self, db: Settings) -> None:
        # Idempotency must not turn into "refuses to add anything".
        _drop_in_inbox(db, "a.fit", fit_builder.build_run(start=_dt("2026-03-15T07:00:00")))
        _drop_in_inbox(db, "b.fit", fit_builder.build_run(start=_dt("2026-03-17T07:00:00")))
        report = import_inbox(db)

        assert report.imported == 2
        files, activities, _, _ = _counts(db)
        assert files == 2
        assert activities == 2


class TestMultisportPersistence:
    def test_parent_and_legs_all_reach_the_database(self, db: Settings) -> None:
        _drop_in_inbox(db, "tri.fit", fit_builder.build_triathlon())
        import_inbox(db)

        with duckdb.connect(str(db.db_path), read_only=True) as conn:
            total = conn.execute("SELECT count(*) FROM activities").fetchone()[0]
            parents = conn.execute(
                "SELECT count(*) FROM activities WHERE parent_activity_id IS NULL"
            ).fetchone()[0]
            legs = conn.execute(
                "SELECT count(*) FROM activities WHERE parent_activity_id IS NOT NULL"
            ).fetchone()[0]

        assert total == 6
        assert parents == 1
        assert legs == 5

    def test_running_volume_includes_the_leg_inside_a_triathlon(self, db: Settings) -> None:
        # The reason the parent/child split exists at all.
        _drop_in_inbox(db, "tri.fit", fit_builder.build_triathlon())
        import_inbox(db)

        with duckdb.connect(str(db.db_path), read_only=True) as conn:
            km = conn.execute(
                "SELECT sum(total_distance_m) / 1000 FROM activities WHERE sport = 'running'"
            ).fetchone()[0]
        assert km == pytest.approx(10.0)

    def test_totals_are_not_double_counted_at_parent_level(self, db: Settings) -> None:
        # Summing every row would count the triathlon twice: once as the
        # parent, once across its legs. Top-level rows only.
        _drop_in_inbox(db, "tri.fit", fit_builder.build_triathlon())
        import_inbox(db)

        with duckdb.connect(str(db.db_path), read_only=True) as conn:
            top_level = conn.execute(
                """
                SELECT sum(total_distance_m) / 1000 FROM activities
                WHERE parent_activity_id IS NULL
                """
            ).fetchone()[0]
        assert top_level == pytest.approx(51.5)


class TestFailureHandling:
    def test_a_corrupt_file_is_recorded_and_quarantined(self, db: Settings) -> None:
        _drop_in_inbox(db, "broken.fit", fit_builder.build_corrupt())
        report = import_inbox(db)

        assert report.failed == 1
        assert (db.inbox_failed_dir / "broken.fit").exists()

        with duckdb.connect(str(db.db_path), read_only=True) as conn:
            status, error = conn.execute("SELECT status, error FROM files").fetchone()
        assert status == "failed"
        assert error

    def test_one_bad_file_does_not_stop_the_batch(self, db: Settings) -> None:
        _drop_in_inbox(db, "broken.fit", fit_builder.build_corrupt())
        _drop_in_inbox(db, "good.fit", fit_builder.build_run())
        report = import_inbox(db)

        assert report.failed == 1
        assert report.imported == 1
        assert (db.inbox_processed_dir / "good.fit").exists()

    def test_processed_files_leave_the_inbox(self, db: Settings) -> None:
        _drop_in_inbox(db, "run.fit", fit_builder.build_run())
        import_inbox(db)

        assert not (db.inbox_dir / "run.fit").exists()
        assert (db.inbox_processed_dir / "run.fit").exists()

    def test_a_failure_mid_transaction_rolls_the_activity_back(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash between the activity insert and its samples must undo both.

        Failing here is the dangerous case: the activity row is already in,
        the records are not, and the result is a session that silently reports
        an empty stream. The whole write is one transaction precisely so that
        cannot happen.
        """
        from garmin_mcp.ingest import writer

        def explode_after_the_activity_insert(_activity):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated disk failure")

        monkeypatch.setattr(writer, "_record_rows", explode_after_the_activity_insert)

        path = _drop_in_inbox(db, "run.fit", fit_builder.build_run())
        with writing(db) as conn:
            outcome = ingest_path(conn, path, settings=db)

        # The error surfaces as a failed outcome, not a corrupt database.
        assert outcome.status == "failed"
        assert "simulated disk failure" in (outcome.reason or "")

        with duckdb.connect(str(db.db_path), read_only=True) as conn:
            assert conn.execute("SELECT count(*) FROM activities").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM laps").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == 0


def _dt(iso: str):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    return datetime.fromisoformat(iso).replace(tzinfo=UTC)

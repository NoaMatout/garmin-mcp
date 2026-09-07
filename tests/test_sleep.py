"""Tests for nightly sleep: parsing, upserting, syncing, and the MCP tool.

No network here, same rule as test_garmin_sync.py. `health_reader.fetch_sleep`
is monkeypatched to stand in for Garmin, since the module it lives in resumes
a real credentialed session and has no `ActivitySource`-style fake to swap in.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import duckdb
import pytest

from garmin_mcp.config import Settings
from garmin_mcp.db.migrations import init_database
from garmin_mcp.errors import GarminAuthError, GarminRateLimitError
from garmin_mcp.ingest.pipeline import sync_sleep_from_garmin
from garmin_mcp.ingest.sleep_parser import parse_sleep
from garmin_mcp.ingest.sleep_writer import write_sleep
from garmin_mcp.ingest.worker import IngestWorker


def _raw_night(
    *,
    total_s: int = 27_120,
    score: int = 78,
    quality: str = "GOOD",
    start_gmt_ms: int = 1_757_130_600_000,
) -> dict[str, Any]:
    """Shaped like a real response, not the (partly wrong) `garminconnect`
    typed model: `averageRespirationValue` inside the DTO, `avgOvernightHrv`
    at the top level — see sleep_parser's module docstring for how that was
    found out the hard way, against a real account.
    """
    return {
        "dailySleepDTO": {
            "sleepTimeSeconds": total_s,
            "napTimeSeconds": 0,
            "deepSleepSeconds": 5400,
            "lightSleepSeconds": 15000,
            "remSleepSeconds": 6000,
            "awakeSleepSeconds": 720,
            "sleepStartTimestampGMT": start_gmt_ms,
            "sleepEndTimestampGMT": start_gmt_ms + total_s * 1000,
            "sleepStartTimestampLocal": start_gmt_ms + 3600 * 1000,
            "sleepEndTimestampLocal": start_gmt_ms + total_s * 1000 + 3600 * 1000,
            "averageRespirationValue": 14.2,
            "lowestRespirationValue": 11.0,
            "highestRespirationValue": 18.0,
            "sleepScores": {"overall": {"value": score, "qualifierKey": quality}},
        },
        "avgOvernightHrv": 45.2,
        "hrvStatus": "BALANCED",
        "restingHeartRate": 47,
        "bodyBatteryChange": 55,
    }


@pytest.fixture
def db(settings: Settings) -> Settings:
    init_database(settings)
    return settings


def _nights(settings: Settings) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        return conn.execute(
            "SELECT calendar_date, total_sleep_s, sleep_score FROM sleep_sessions "
            "ORDER BY calendar_date"
        ).fetchall()


class TestParser:
    def test_parses_a_full_payload(self) -> None:
        session = parse_sleep(_raw_night(), date(2026, 9, 6))
        assert session is not None
        assert session.total_sleep_s == 27_120
        assert session.sleep_score == 78
        assert session.sleep_quality == "GOOD"
        assert session.deep_sleep_s == 5400
        assert session.sleep_start_utc is not None
        # Local timestamps are stored naive, matching every other *_local column.
        assert session.sleep_start_local is not None
        assert session.sleep_start_local.tzinfo is None

    def test_hrv_is_read_from_the_top_level_not_the_dto(self) -> None:
        """Regression: the first cut read `dailySleepDTO.avgSleepHRV`, which
        does not exist against a real account — HRV lives at the response's
        top level as `avgOvernightHrv`, and that field silently stayed None
        for every night until this was caught by inspecting real data."""
        session = parse_sleep(_raw_night(), date(2026, 9, 6))
        assert session is not None
        assert session.avg_hrv == 45.2
        assert session.hrv_status == "BALANCED"

    def test_respiration_key_has_the_missing_e(self) -> None:
        """Regression: the DTO field is `averageRespirationValue`, not the
        `avgRespirationValue` the typed client model — and the first cut of
        this parser — used."""
        session = parse_sleep(_raw_night(), date(2026, 9, 6))
        assert session is not None
        assert session.avg_respiration == 14.2

    def test_recovery_context_fields_are_captured(self) -> None:
        session = parse_sleep(_raw_night(), date(2026, 9, 6))
        assert session is not None
        assert session.resting_heart_rate == 47
        assert session.body_battery_change == 55

    def test_spo2_absent_on_a_device_without_a_pulse_ox_sensor_stays_none(self) -> None:
        """Not every field that comes back None is a mapping bug — SpO2
        genuinely is not in the payload for a watch with no pulse-ox sensor,
        and the fixture reflects that rather than fabricating a value."""
        session = parse_sleep(_raw_night(), date(2026, 9, 6))
        assert session is not None
        assert session.avg_spo2 is None

    def test_a_night_with_no_detected_sleep_is_none(self) -> None:
        assert parse_sleep({"dailySleepDTO": {"sleepTimeSeconds": None}}, date(2026, 9, 6)) is None
        assert parse_sleep({"dailySleepDTO": {}}, date(2026, 9, 6)) is None

    def test_a_missing_dto_is_none_not_a_crash(self) -> None:
        assert parse_sleep({}, date(2026, 9, 6)) is None
        assert parse_sleep(None, date(2026, 9, 6)) is None

    def test_the_raw_payload_is_kept_in_extra(self) -> None:
        raw = _raw_night()
        session = parse_sleep(raw, date(2026, 9, 6))
        assert session is not None
        assert session.extra == raw


class TestWriter:
    def test_write_then_write_again_upserts_in_place(self, db: Settings) -> None:
        first = parse_sleep(_raw_night(total_s=27_000, score=70), date(2026, 9, 6))
        assert first is not None
        with duckdb.connect(str(db.db_path)) as conn:
            write_sleep(conn, first)

        second = parse_sleep(_raw_night(total_s=28_800, score=85), date(2026, 9, 6))
        assert second is not None
        with duckdb.connect(str(db.db_path)) as conn:
            write_sleep(conn, second)

        rows = _nights(db)
        assert len(rows) == 1  # replaced, not duplicated
        assert rows[0][1] == 28_800
        assert rows[0][2] == 85


class TestSyncSleepFromGarmin:
    def test_fetches_and_writes_the_configured_window(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        today = date(2026, 9, 7)
        payloads = {today - timedelta(days=i): _raw_night() for i in range(3)}

        def fake_fetch(days: list[date], _settings: Settings | None = None) -> dict[date, Any]:
            assert set(days) == set(payloads)
            return payloads

        monkeypatch.setattr("garmin_mcp.garmin.health_reader.fetch_sleep", fake_fetch)

        report = sync_sleep_from_garmin(db, days=3, today=today)

        assert report.upserted == 3
        assert report.failed == 0
        assert len(_nights(db)) == 3

    def test_a_night_the_watch_saw_nothing_for_is_not_stored(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        today = date(2026, 9, 7)
        monkeypatch.setattr(
            "garmin_mcp.garmin.health_reader.fetch_sleep",
            lambda days, _settings=None: {d: {"dailySleepDTO": {}} for d in days},
        )

        report = sync_sleep_from_garmin(db, days=2, today=today)

        assert report.upserted == 0
        assert _nights(db) == []

    def test_a_fetch_failure_for_one_night_is_counted_not_raised(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        today = date(2026, 9, 7)

        def flaky(days: list[date], _settings: Settings | None = None) -> dict[date, Any]:
            return {d: (None if i == 0 else _raw_night()) for i, d in enumerate(days)}

        monkeypatch.setattr("garmin_mcp.garmin.health_reader.fetch_sleep", flaky)

        report = sync_sleep_from_garmin(db, days=2, today=today)

        assert report.upserted == 1
        assert report.failed == 1

    def test_zero_days_is_a_no_op_that_touches_no_session(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*_a: object, **_k: object) -> None:
            raise AssertionError("must not resume a session when the window is 0")

        monkeypatch.setattr("garmin_mcp.garmin.health_reader.fetch_sleep", explode)

        report = sync_sleep_from_garmin(db, days=0)

        assert report.upserted == 0
        assert _nights(db) == []

    def test_a_rate_limit_propagates_rather_than_being_swallowed(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_rate_limit(
            _days: list[date], _settings: Settings | None = None
        ) -> dict[date, Any]:
            raise GarminRateLimitError("slow down")

        monkeypatch.setattr("garmin_mcp.garmin.health_reader.fetch_sleep", raise_rate_limit)

        with pytest.raises(GarminRateLimitError):
            sync_sleep_from_garmin(db, days=2)


class TestWorkerIntegration:
    def test_a_successful_cycle_includes_a_sleep_report(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.ingest.pipeline import SleepSyncReport

        worker = IngestWorker(db)
        monkeypatch.setattr(
            IngestWorker, "_sync_garmin", lambda _self, **_k: _EmptyIngestReport()
        )
        monkeypatch.setattr(
            IngestWorker, "_sync_sleep", lambda _self: SleepSyncReport(upserted=2, failed=0)
        )

        outcome = worker._run_cycle(limit=None)

        assert outcome["sleep"]["upserted"] == 2
        assert "sleep_error" not in outcome

    def test_sleep_sync_is_skipped_when_the_activity_sync_already_failed(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken session would fail sleep sync identically — no point asking twice."""
        worker = IngestWorker(db)

        def broken(*_a: object, **_k: object) -> None:
            raise GarminAuthError("no saved Garmin session")

        def explode(_self: IngestWorker) -> None:
            raise AssertionError("sleep sync must not run after garmin sync already failed")

        monkeypatch.setattr(IngestWorker, "_sync_garmin", broken)
        monkeypatch.setattr(IngestWorker, "_sync_sleep", explode)

        outcome = worker._run_cycle(limit=None)

        assert "garmin_error" in outcome
        assert "sleep" not in outcome
        assert "sleep_error" not in outcome


class _EmptyIngestReport:
    imported = 0
    skipped = 0
    failed = 0
    activities = 0
    records = 0

    def summary(self) -> str:
        return "0 imported"


class TestSleepHistoryTool:
    def _seed(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        from garmin_mcp.server import tools

        with duckdb.connect(str(settings.db_path)) as conn:
            for i in range(3):
                session = parse_sleep(_raw_night(total_s=25_000 + i * 1000), date(2026, 9, 4 + i))
                assert session is not None
                write_sleep(conn, session)
        monkeypatch.setattr(tools, "_settings", lambda: settings)

    def test_lists_nights_most_recent_first(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        self._seed(db, monkeypatch)
        result = tools.sleep_history()

        assert result["count"] == 3
        dates = [n["date"] for n in result["nights"]]
        assert dates == sorted(dates, reverse=True)
        assert "duration" in result["nights"][0]

    def test_since_filters_the_window(self, db: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        from garmin_mcp.server import tools

        self._seed(db, monkeypatch)
        result = tools.sleep_history(since="2026-09-05")

        assert result["count"] == 2

    def test_database_status_reports_sleep_coverage(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        self._seed(db, monkeypatch)
        status = tools.database_status()

        assert status["sleep"]["nights_recorded"] == 3
        assert status["sleep"]["last_night"] == "2026-09-06"

    def test_no_sleep_data_yet_reports_zero_not_an_error(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        monkeypatch.setattr(tools, "_settings", lambda: db)
        status = tools.database_status()
        assert status["sleep"]["nights_recorded"] == 0

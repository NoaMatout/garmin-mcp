"""Tests for get_capabilities and the sync-state record behind it.

The distinction under test is the one a product depends on: a capability being
*implemented* is not the same as it having been *verified*, and a Garmin session
that has expired must degrade the second without touching local reads.

No network, no Garmin. The worker's cycle is driven with its Garmin side
replaced, exactly as in test_worker.py.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from garmin_mcp.config import Settings
from garmin_mcp.db.connection import writing
from garmin_mcp.db.migrations import init_database
from garmin_mcp.errors import GarminAuthError, GarminBlockedError, GarminError
from garmin_mcp.ingest import sync_state
from garmin_mcp.ingest.pipeline import CourseSyncReport, IngestReport, SleepSyncReport
from garmin_mcp.ingest.worker import IngestWorker
from garmin_mcp.server import capabilities, route_tools
from tests.geo_fixtures import line, seed_activity


@pytest.fixture
def db(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    init_database(settings)
    monkeypatch.setattr(capabilities, "_settings", lambda: settings)
    monkeypatch.setattr(route_tools, "_settings", lambda: settings)
    return settings


def _caps(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in result["capabilities"]}


def _issue_codes(result: dict[str, Any]) -> set[str]:
    return {i["code"] for i in result["issues"]}


def _good_cycle() -> dict[str, Any]:
    return {
        "garmin": {"summary": "0 new"},
        "sleep": {"summary": "3 nights"},
        "courses": {"summary": "14 courses"},
    }


class TestFreshInstallation:
    def test_nothing_is_claimed_that_has_not_been_observed(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(capabilities, "_settings", lambda: settings)
        result = capabilities.get_capabilities()

        assert result["local_history"]["readable"] is False
        assert result["garmin_access"]["status"] == "unknown"
        assert result["garmin_access"]["verified"] is False
        assert result["sync"]["data_freshness"] == "unknown"
        assert {"database_missing", "worker_not_running", "writes_disabled"} <= _issue_codes(result)

    def test_every_issue_carries_a_code_a_severity_and_a_recovery(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(capabilities, "_settings", lambda: settings)
        for issue in capabilities.get_capabilities()["issues"]:
            assert issue["code"] and issue["recovery"]
            assert issue["severity"] in {"error", "warning", "info"}
            assert issue["component"]

    def test_implemented_and_verified_are_reported_separately(self, db: Settings) -> None:
        caps = _caps(capabilities.get_capabilities())
        notes = caps["activities.write_notes"]
        assert notes["implemented"] is True
        assert notes["enabled"] is False  # writes are off by default
        assert notes["verification"]["state"] == "not_verified"

    def test_gpx_course_import_is_declared_not_implemented(self, db: Settings) -> None:
        importer = _caps(capabilities.get_capabilities())["courses.import_gpx"]
        assert importer["implemented"] is False and importer["available_now"] is False
        assert "docs/garmin-courses.md" in importer["note"]


class TestLocalHistory:
    def test_a_populated_database_is_readable_and_described(self, db: Settings) -> None:
        with writing(db) as conn:
            seed_activity(conn, 1, line(10))
            seed_activity(conn, 2, None, start=None, sub_sport="treadmill")
        history = capabilities.get_capabilities()["local_history"]
        assert history["readable"] is True
        assert history["activities"] == 2 and history["activities_with_start_position"] == 1
        assert history["schema_version"] >= 3

    def test_local_features_are_available_without_a_worker_or_garmin(self, db: Settings) -> None:
        caps = _caps(capabilities.get_capabilities())
        for name in ("history.read", "history.search_by_location", "history.route_geometry"):
            assert caps[name]["available_now"] is True
        assert caps["sync.activities"]["available_now"] is False  # no worker running


class TestExpiredGarminSession:
    @pytest.fixture
    def expired(self, db: Settings) -> Settings:
        sync_state.record_cycle(db, _good_cycle())
        sync_state.record_cycle(
            db,
            {"garmin_error": "x", "garmin_error_code": GarminAuthError.code},
        )
        IngestWorker(db)._heartbeat()
        return db

    def test_the_status_says_what_happened_and_when_it_last_worked(self, expired: Settings) -> None:
        access = capabilities.get_capabilities()["garmin_access"]
        assert access["status"] == "auth_required" and access["verified"] is False
        assert access["error_code"] == "garmin_auth_required"
        assert access["observed_at"] and access["last_ok_at"]
        assert access["last_ok_at"] <= access["observed_at"]

    def test_the_recovery_action_is_actionable(self, expired: Settings) -> None:
        issue = next(
            i
            for i in capabilities.get_capabilities()["issues"]
            if i["code"] == "garmin_auth_required"
        )
        assert issue["severity"] == "error" and "garmin-mcp auth" in issue["recovery"]

    def test_local_reads_stay_available_and_garmin_features_do_not(self, expired: Settings) -> None:
        caps = _caps(capabilities.get_capabilities())
        assert caps["history.read"]["available_now"] is True
        assert caps["history.search_by_location"]["available_now"] is True
        assert caps["sync.activities"]["available_now"] is False
        assert caps["sync.activities"]["verification"]["state"] == "verified_failing"

    def test_local_tools_actually_work_in_that_state(
        self, expired: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with writing(expired) as conn:
            seed_activity(conn, 1, line(20))

        def refuse(*_a: object, **_k: object) -> None:
            raise AssertionError("tried to reach the network")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        found = route_tools.find_local_activities(49.2583, 4.0317, radius_km=5)
        assert found["total_matches"] == 1
        assert route_tools.get_activity_route(1)["gpx"]["points"] == 20
        assert capabilities.get_capabilities()["local_history"]["readable"] is True

    def test_recovery_clears_the_failure_and_keeps_the_history(self, expired: Settings) -> None:
        sync_state.record_cycle(expired, _good_cycle())
        access = capabilities.get_capabilities()["garmin_access"]
        assert access["status"] == "ok" and access["verified"] is True
        assert access["error_code"] is None


class TestFreshnessAndVerification:
    def test_a_recent_successful_sync_is_fresh_and_verifies_the_reads(self, db: Settings) -> None:
        sync_state.record_cycle(db, _good_cycle())
        IngestWorker(db)._heartbeat()
        result = capabilities.get_capabilities()

        assert result["sync"]["data_freshness"] == "fresh"
        assert result["garmin_access"]["verified"] is True
        caps = _caps(result)
        for name in ("sync.activities", "sync.sleep", "courses.read"):
            assert caps[name]["verification"]["state"] == "verified_ok"
            assert caps[name]["verification"]["observed_at"]

    def test_an_old_success_is_stale(self, db: Settings) -> None:
        sync_state.record_cycle(db, _good_cycle())
        path = db.state_dir / sync_state.STATE_FILENAME
        state = json.loads(path.read_text())
        state["last_success_at"] = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
        path.write_text(json.dumps(state))

        result = capabilities.get_capabilities()
        assert result["sync"]["data_freshness"] == "stale"
        assert "data_stale" in _issue_codes(result)

    def test_a_failure_does_not_erase_the_last_success(self, db: Settings) -> None:
        sync_state.record_cycle(db, _good_cycle())
        first = sync_state.read_state(db)["last_success_at"]
        sync_state.record_cycle(db, {"garmin_error": "x", "garmin_error_code": "garmin_blocked"})
        state = sync_state.read_state(db)
        assert state["last_success_at"] == first
        assert state["garmin"]["status"] == "blocked"
        assert state["garmin"]["last_ok_at"] == first
        assert state["components"]["activities"]["ok"] is False
        assert state["components"]["activities"]["last_ok_at"] == first

    def test_a_skipped_component_keeps_its_previous_observation(self, db: Settings) -> None:
        sync_state.record_cycle(db, _good_cycle())
        before = sync_state.read_state(db)["components"]["sleep"]["observed_at"]
        # The activity sync failed, so sleep and courses were not attempted.
        sync_state.record_cycle(db, {"garmin_error": "x", "garmin_error_code": "garmin_blocked"})
        assert sync_state.read_state(db)["components"]["sleep"]["observed_at"] == before

    def test_a_failing_course_sync_is_a_warning_not_an_outage(self, db: Settings) -> None:
        sync_state.record_cycle(
            db,
            {"garmin": {}, "sleep": {}, "courses_error": "x", "courses_error_code": "garmin_error"},
        )
        IngestWorker(db)._heartbeat()
        result = capabilities.get_capabilities()
        assert result["garmin_access"]["status"] == "ok"
        assert "courses_sync_failing" in _issue_codes(result)
        assert _caps(result)["courses.read"]["verification"]["state"] == "verified_failing"

    def test_a_corrupt_state_file_means_nothing_observed_not_a_crash(self, db: Settings) -> None:
        (db.state_dir / sync_state.STATE_FILENAME).write_text("{ not json")
        assert capabilities.get_capabilities()["garmin_access"]["status"] == "unknown"


class TestWrites:
    def test_a_write_is_verified_only_once_one_has_succeeded(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db, "enable_writes", True)
        IngestWorker(db)._heartbeat()

        before = _caps(capabilities.get_capabilities())["workouts.write"]
        assert before["enabled"] is True and before["implemented"] is True
        assert before["verification"]["state"] == "not_verified"

        sync_state.record_operation(db, "workout.create", ok=True)
        after = _caps(capabilities.get_capabilities())["workouts.write"]
        assert after["verification"]["state"] == "verified_ok"

    def test_a_failed_write_is_reported_failing(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db, "enable_writes", True)
        sync_state.record_operation(db, "activity.set_notes", ok=False, error_code="request_failed")
        notes = _caps(capabilities.get_capabilities())["activities.write_notes"]
        assert notes["verification"]["state"] == "verified_failing"

    def test_a_successful_request_also_proves_garmin_access(self, db: Settings) -> None:
        sync_state.record_operation(db, "workout.list", ok=True)
        assert sync_state.read_state(db)["garmin"]["status"] == "ok"

    def test_an_invalid_workout_says_nothing_about_garmin_access(self, db: Settings) -> None:
        sync_state.record_cycle(db, _good_cycle())
        sync_state.record_operation(db, "workout.create", ok=False, error_code="request_failed")
        assert sync_state.read_state(db)["garmin"]["status"] == "ok"

    def test_an_auth_failure_on_a_write_does_mark_access(self, db: Settings) -> None:
        sync_state.record_cycle(db, _good_cycle())
        sync_state.record_operation(
            db, "workout.create", ok=False, error_code="garmin_auth_required"
        )
        assert sync_state.read_state(db)["garmin"]["status"] == "auth_required"


class TestWorkerRecordsWhatItSees:
    def _worker(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch, **behaviours: Any
    ) -> IngestWorker:
        def garmin(_self: object, **_kw: object) -> IngestReport:
            outcome = behaviours.get("garmin", IngestReport())
            if isinstance(outcome, Exception):
                raise outcome
            return outcome  # type: ignore[no-any-return]

        monkeypatch.setattr(IngestWorker, "_sync_garmin", garmin)
        monkeypatch.setattr(IngestWorker, "_sync_sleep", lambda _s: SleepSyncReport())
        monkeypatch.setattr(IngestWorker, "_sync_courses", lambda _s: CourseSyncReport())
        return IngestWorker(db)

    def test_a_good_cycle_is_recorded_as_a_success(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._worker(db, monkeypatch)._run_cycle(limit=None)
        state = sync_state.read_state(db)
        assert state["last_success_at"] and state["garmin"]["status"] == "ok"
        assert set(state["components"]) == {"activities", "sleep", "courses"}

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (GarminAuthError("no session"), "auth_required"),
            (GarminBlockedError("403"), "blocked"),
            (GarminError("down"), "unavailable"),
            (RuntimeError("boom"), "failed"),
        ],
    )
    def test_each_kind_of_failure_is_classified(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch, error: Exception, status: str
    ) -> None:
        self._worker(db, monkeypatch, garmin=error)._run_cycle(limit=None)
        state = sync_state.read_state(db)
        assert state["garmin"]["status"] == status
        assert state["last_success_at"] is None and state["last_attempt_at"]

    def test_garmin_error_text_is_never_stored(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "Authorization: Bearer abc123-super-secret"
        self._worker(db, monkeypatch, garmin=GarminError(secret))._run_cycle(limit=None)
        assert "abc123" not in (db.state_dir / sync_state.STATE_FILENAME).read_text()

    def test_serving_a_workout_request_records_the_operation(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.garmin import workout_writer
        from garmin_mcp.ingest.worker import REQUEST_SUFFIX

        monkeypatch.setattr(workout_writer, "list_workouts", lambda *_a, **_k: [])
        worker = self._worker(db, monkeypatch)
        (db.trigger_dir / f"workout-r1{REQUEST_SUFFIX}").write_text(json.dumps({"action": "list"}))
        worker._serve_requests()
        assert "workout.list" in sync_state.read_state(db)["operations"]

    def test_the_record_never_breaks_the_cycle(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An advisory file that cannot be written must not stop syncing."""
        monkeypatch.setattr(
            sync_state, "_write", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full"))
        )
        outcome = self._worker(db, monkeypatch)._run_cycle(limit=None)
        assert "garmin" in outcome


class TestNothingSecretIsExposed:
    def test_no_credential_token_or_path_appears_in_the_output(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db, "email", "athlete@example.invalid")
        monkeypatch.setattr(db, "enable_writes", True)
        (db.token_dir / "oauth1_token.json").write_text('{"oauth_token": "tok-SECRET-1"}')
        sync_state.record_cycle(db, _good_cycle())
        sync_state.record_cycle(
            db, {"garmin_error": "tok-SECRET-1", "garmin_error_code": "garmin_blocked"}
        )
        IngestWorker(db)._heartbeat()

        dumped = json.dumps(capabilities.get_capabilities(), default=str)
        assert "SECRET" not in dumped
        assert "athlete@example.invalid" not in dumped
        assert str(db.token_dir) not in dumped
        assert str(db.db_path) not in dumped and str(db.data_dir) not in dumped
        assert capabilities.get_capabilities()["garmin_access"]["session_saved"] is True

    def test_the_tool_makes_no_network_call(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_a: object, **_k: object) -> None:
            raise AssertionError("get_capabilities tried to reach the network")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        assert capabilities.get_capabilities()["observation_note"]


class TestErrorsAsData:
    def test_every_error_class_has_a_distinct_stable_code(self) -> None:
        from garmin_mcp import errors

        classes = [
            c
            for c in vars(errors).values()
            if isinstance(c, type) and issubclass(c, errors.GarminMcpError)
        ]
        codes = [c.code for c in classes if c is not errors.GarminMcpError]
        assert len(codes) == len(set(codes))

    def test_an_error_serialises_with_its_recovery(self) -> None:
        payload = GarminAuthError("expired").as_dict()
        assert payload["code"] == "garmin_auth_required"
        assert payload["message"] == "expired"
        assert "garmin-mcp auth" in (payload["recovery"] or "")

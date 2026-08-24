"""Tests for comparing a prescribed session against what was run.

The pairing itself needs no testing of ours — the watch records which
prescribed step each lap belongs to, so it is data rather than inference.
What does need testing is the reading of it: repeats expressed as a step that
jumps backwards, reps that were not completed, and drift across a set, which
a single average hides.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from garmin_mcp.config import Settings
from garmin_mcp.db.migrations import init_database
from garmin_mcp.ingest.pipeline import import_inbox
from garmin_mcp.server import formatters, tools
from tests import fit_builder

# 20 min warmup, then 12 x (1 min active, 1 min recovery), then 5 min cooldown —
# the shape of the real session this was built against.
PLAN = [
    {
        "step_index": 0,
        "workout_name": "Intervals",
        "intensity": "warmup",
        "duration_type": "time",
        "duration_value": 1200.0,
    },
    {
        "step_index": 1,
        "workout_name": "Intervals",
        "intensity": "active",
        "duration_type": "time",
        "duration_value": 60.0,
    },
    {
        "step_index": 2,
        "workout_name": "Intervals",
        "intensity": "recovery",
        "duration_type": "time",
        "duration_value": 60.0,
    },
    {
        "step_index": 3,
        "workout_name": "Intervals",
        "duration_type": "repeat_until_steps_cmplt",
        "repeat_from_step": 1,
        "repeat_count": 12,
    },
    {
        "step_index": 4,
        "workout_name": "Intervals",
        "intensity": "cooldown",
        "duration_type": "time",
        "duration_value": 300.0,
    },
]


def _lap(step: int, index: int, speed: float, duration: float = 60.0) -> dict[str, Any]:
    return {
        "wkt_step_index": step,
        "lap_index": index,
        "total_timer_time_s": duration,
        "total_distance_m": speed * duration,
        "avg_speed_mps": speed,
        "avg_heart_rate": 170,
        "intensity": "active",
    }


def _laps(active_speeds: list[float], recoveries: int = 12) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {
        0: [_lap(0, i, 3.2, 300.0) for i in range(4)],
        1: [_lap(1, 10 + i, s) for i, s in enumerate(active_speeds)],
        2: [_lap(2, 30 + i, 1.6) for i in range(recoveries)],
        4: [_lap(4, 90, 3.3, 300.0)],
    }
    return grouped


ACTIVITY = {"activity_id": 1, "sport": "running", "start_time_local": None}


class TestPlanReading:
    def test_a_repeat_step_is_not_reported_as_something_to_run(self) -> None:
        # It is structural: a jump backwards, not a block the athlete runs.
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.5] * 12))
        assert [b["step"] for b in result["blocks"]] == [
            "warmup",
            "active",
            "recovery",
            "cooldown",
        ]

    def test_repeated_steps_carry_their_prescribed_count(self) -> None:
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.5] * 12))
        active = result["blocks"][1]
        assert active["prescribed_reps"] == 12
        assert active["completed_reps"] == 12
        assert "note" not in active

    def test_a_missing_rep_is_called_out(self) -> None:
        """Skipping the last recovery is normal; silently reporting 12 is not."""
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.5] * 12, recoveries=11))
        recovery = result["blocks"][2]
        assert recovery["completed_reps"] == 11
        assert "11 of 12" in recovery["note"]

    def test_an_unrepeated_step_merges_its_laps(self) -> None:
        # Auto-lap splits a 20-minute warm-up into kilometres; the plan asked
        # for one block, so it reads as one block.
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.5] * 12))
        warmup = result["blocks"][0]
        assert warmup["laps_merged"] == 4
        assert warmup["executed"]["duration"] == "20:00"

    def test_a_step_with_no_laps_says_so(self) -> None:
        laps = _laps([4.5] * 12)
        del laps[4]
        result = formatters.compare_to_plan(ACTIVITY, PLAN, laps)
        assert result["blocks"][3]["note"] == "no lap recorded for this step"


class TestDrift:
    """A single average hides the thing a coach looks for first."""

    def test_a_steady_set_reads_as_held(self) -> None:
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.5] * 12))
        assert result["blocks"][1]["drift_verdict"] == "held"

    def test_fading_across_the_set_is_detected(self) -> None:
        # Fast first half, slower second: the classic blow-up.
        speeds = [4.7] * 6 + [4.1] * 6
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps(speeds))
        active = result["blocks"][1]
        assert active["drift_verdict"] == "faded"
        assert active["drift_s_per_km"] > 2

    def test_a_negative_split_is_distinguished_from_fading(self) -> None:
        speeds = [4.1] * 6 + [4.7] * 6
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps(speeds))
        assert result["blocks"][1]["drift_verdict"] == "negative split"

    def test_average_alone_cannot_tell_them_apart(self) -> None:
        """The justification for reporting drift at all."""
        faded = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.7] * 6 + [4.1] * 6))
        negative = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.1] * 6 + [4.7] * 6))
        assert faded["blocks"][1]["avg_pace"] == negative["blocks"][1]["avg_pace"]
        assert faded["blocks"][1]["drift_verdict"] != negative["blocks"][1]["drift_verdict"]


class TestTool:
    @pytest.fixture
    def db(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
        init_database(settings)
        (settings.inbox_dir / "run.fit").write_bytes(fit_builder.build_run())
        import_inbox(settings)
        monkeypatch.setattr(tools, "_settings", lambda: settings)
        return settings

    def test_a_session_without_a_plan_explains_itself(self, db: Settings) -> None:
        # Most runs are not structured workouts; that is not an error.
        with duckdb.connect(str(db.db_path), read_only=True) as conn:
            activity_id = conn.execute("SELECT activity_id FROM activities").fetchone()[0]

        result = tools.compare_to_plan(activity_id)
        assert result["has_plan"] is False
        assert "not run from a structured workout" in result["note"]

    def test_an_unknown_activity_raises(self, db: Settings) -> None:
        from garmin_mcp.errors import ActivityNotFoundError

        with pytest.raises(ActivityNotFoundError):
            tools.compare_to_plan(999999)


class TestVerdictsPerStepKind:
    """A recovery is not a failed interval.

    With 45 seconds between 400s the athlete is deliberately not recovering,
    so reporting the recoveries as "faded" reads as a contre-performance when
    it is the design of the session. Observed on a real week.
    """

    def test_work_steps_get_a_pace_drift_verdict(self) -> None:
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.7] * 6 + [4.1] * 6))
        assert result["blocks"][1]["drift_verdict"] == "faded"

    def test_recovery_steps_get_no_pace_verdict(self) -> None:
        result = formatters.compare_to_plan(ACTIVITY, PLAN, _laps([4.5] * 12))
        recovery = result["blocks"][2]
        assert "drift_verdict" not in recovery
        assert "drift_s_per_km" not in recovery

    def test_recovery_reports_heart_rate_trend_instead(self) -> None:
        """What carries information there is whether HR came back down."""
        laps = _laps([4.5] * 12)
        for index, lap in enumerate(laps[2]):
            lap["avg_heart_rate"] = 170 + index  # climbing through the set
        result = formatters.compare_to_plan(ACTIVITY, PLAN, laps)
        recovery = result["blocks"][2]
        assert recovery["hr_trend_bpm"] > 2
        assert recovery["recovery_quality"] == "incomplete"

    def test_a_recovery_that_comes_back_down_reads_as_improving(self) -> None:
        laps = _laps([4.5] * 12)
        for index, lap in enumerate(laps[2]):
            lap["avg_heart_rate"] = 185 - index
        result = formatters.compare_to_plan(ACTIVITY, PLAN, laps)
        assert result["blocks"][2]["recovery_quality"] == "improving"


class TestConformity:
    """Counting reps reports presence, not whether they were done as asked."""

    def test_a_short_rep_in_the_middle_is_flagged(self) -> None:
        laps = _laps([4.5] * 12)
        laps[1][5]["total_timer_time_s"] = 20.0  # prescribed 60
        result = formatters.compare_to_plan(ACTIVITY, PLAN, laps)
        assert result["blocks"][1]["short_reps"] == [6]

    def test_only_the_last_rep_short_is_normal_not_a_shortfall(self) -> None:
        # Cutting the final recovery to start the cool-down is routine.
        laps = _laps([4.5] * 12)
        laps[2][-1]["total_timer_time_s"] = 30.0
        recovery = formatters.compare_to_plan(ACTIVITY, PLAN, laps)["blocks"][2]
        assert "short_reps" not in recovery
        assert "cut short" in recovery["note_last_rep"]

    def test_warmup_and_cooldown_are_never_flagged(self) -> None:
        """The athlete does what they like around the session."""
        laps = _laps([4.5] * 12)
        laps[0] = [_lap(0, 0, 3.2, 60.0)]  # 1 min instead of the prescribed 20
        laps[4] = [_lap(4, 90, 3.3, 60.0)]
        result = formatters.compare_to_plan(ACTIVITY, PLAN, laps)
        assert "short_reps" not in result["blocks"][0]
        assert "short_reps" not in result["blocks"][3]


class TestCalendarAdherence:
    """Planned against done, at the level of the week rather than the session.

    Found in real use: a session was scheduled, the athlete ran it almost
    exactly as written, but the workout never reached the watch over Bluetooth
    so he started a free run instead. Every fact needed to say so was present;
    nothing said it. `compare_to_plan` simply answered "no plan".
    """

    @pytest.fixture
    def db(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
        from datetime import UTC, datetime

        init_database(settings)
        (settings.inbox_dir / "run.fit").write_bytes(
            fit_builder.build_run(start=datetime(2026, 3, 17, 7, 30, tzinfo=UTC))
        )
        import_inbox(settings)
        monkeypatch.setattr(tools, "_settings", lambda: settings)
        return settings

    def _calendar(self, monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]) -> None:
        monkeypatch.setattr(
            "garmin_mcp.ingest.worker.request_workout",
            lambda *_a, **_k: {"scheduled": entries},
        )

    def test_a_session_run_free_is_reported_not_lost(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._calendar(
            monkeypatch,
            [{"date": "2026-03-17", "name": "Tempo 3x2km", "sport": "running"}],
        )
        report = tools.plan_adherence("2026-03-17")

        assert report["completed_off_plan"] == 1
        day = report["days"][0]
        assert day["status"] == "completed_off_plan"
        assert day["planned"] == "Tempo 3x2km"
        assert "not started from the scheduled workout" in day["note"]

    def test_a_planned_day_with_nothing_recorded_is_missed(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._calendar(
            monkeypatch,
            [{"date": "2026-03-19", "name": "Long run", "sport": "running"}],
        )
        report = tools.plan_adherence("2026-03-17")
        assert report["missed"] == 1
        assert report["days"][0]["status"] == "missed"

    def test_an_activity_with_nothing_planned_is_not_an_error(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Strength sessions the coach never scheduled should not read as noise.
        self._calendar(monkeypatch, [])
        report = tools.plan_adherence("2026-03-17")
        assert report["planned"] == 0
        assert len(report["unplanned_activities"]) == 1

    def test_the_week_is_snapped_to_its_monday(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._calendar(monkeypatch, [])
        for day in ("2026-03-16", "2026-03-19", "2026-03-22"):
            assert tools.plan_adherence(day)["week_start"] == "2026-03-16"

    def test_an_unreachable_calendar_degrades_rather_than_fails(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The activity side is still worth reporting on its own."""
        monkeypatch.setattr(
            "garmin_mcp.ingest.worker.request_workout",
            lambda *_a, **_k: {"error": "no ingest worker is running"},
        )
        report = tools.plan_adherence("2026-03-17")
        assert "calendar_unavailable" in report
        assert len(report["unplanned_activities"]) == 1

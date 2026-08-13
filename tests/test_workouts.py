"""Tests for building and sending structured workouts.

This is the only part of the project that modifies the athlete's Garmin
account, so the tests are weighted towards the ways that could go wrong
quietly:

* a malformed session reaching the watch and behaving oddly mid-interval;
* pace targets inverted, which Garmin accepts and the watch ignores;
* a workout being created on a single tool call, with nobody having seen it.

Nothing here touches the network. The upload boundary is mocked; everything
below it is real.
"""

from __future__ import annotations

from typing import Any

import pytest

from garmin_mcp.config import Settings
from garmin_mcp.garmin.workouts import (
    Repeat,
    Step,
    WorkoutSpec,
    WorkoutSpecError,
    build_workout,
    parse_pace,
    spec_from_dict,
)
from garmin_mcp.server import tools

INTERVALS = [
    {"kind": "warmup", "duration_s": 1200},
    {
        "times": 12,
        "steps": [
            {"kind": "interval", "duration_s": 60, "target_pace": "3:45"},
            {"kind": "recovery", "duration_s": 60},
        ],
    },
    {"kind": "cooldown", "duration_s": 600},
]


def _steps_of(workout: Any) -> list[dict[str, Any]]:
    return workout.model_dump(exclude_none=True)["workoutSegments"][0]["workoutSteps"]


class TestPaceParsing:
    @pytest.mark.parametrize(
        ("text", "seconds"), [("3:45", 225), ("3:45/km", 225), (" 4:00 ", 240), ("10:30", 630)]
    )
    def test_accepts_the_forms_a_coach_writes(self, text: str, seconds: int) -> None:
        assert parse_pace(text) == seconds

    @pytest.mark.parametrize("text", ["3.45", "3:5", "fast", "", "3:45/mi"])
    def test_rejects_anything_ambiguous(self, text: str) -> None:
        with pytest.raises(WorkoutSpecError, match="pace must look like"):
            parse_pace(text)

    @pytest.mark.parametrize("text", ["0:45", "1:59", "25:00"])
    def test_rejects_implausible_paces(self, text: str) -> None:
        """A mistyped target is discovered mid-interval, on the watch."""
        with pytest.raises(WorkoutSpecError, match="plausible range"):
            parse_pace(text)


class TestSpecValidation:
    def test_a_step_needs_exactly_one_end_condition(self) -> None:
        # With neither, the step would run until the lap button is pressed —
        # never what a written session means.
        with pytest.raises(WorkoutSpecError, match="exactly one"):
            Step("interval").validate()
        with pytest.raises(WorkoutSpecError, match="exactly one"):
            Step("interval", duration_s=60, distance_m=400).validate()

    def test_negative_durations_are_rejected(self) -> None:
        with pytest.raises(WorkoutSpecError, match="positive"):
            Step("interval", duration_s=-60).validate()

    def test_unknown_step_kind_lists_the_valid_ones(self) -> None:
        with pytest.raises(WorkoutSpecError, match="unknown step kind"):
            Step("sprint", duration_s=60).validate()  # type: ignore[arg-type]

    def test_an_empty_workout_is_rejected(self) -> None:
        with pytest.raises(WorkoutSpecError, match="no steps"):
            WorkoutSpec(name="Empty").validate()

    def test_a_repeat_needs_iterations_and_steps(self) -> None:
        with pytest.raises(WorkoutSpecError, match="at least one iteration"):
            Repeat(times=0, steps=[Step("interval", duration_s=60)]).validate()
        with pytest.raises(WorkoutSpecError, match="at least one step"):
            Repeat(times=4, steps=[]).validate()

    def test_unknown_keys_are_an_error_not_ignored(self) -> None:
        """A mistyped `distance` must not silently become a lap-button step."""
        with pytest.raises(WorkoutSpecError, match="unknown keys"):
            spec_from_dict({"name": "x", "blocks": [{"kind": "interval", "distance": 400}]})


class TestBuiltPayload:
    def test_step_order_is_continuous_across_a_repeat(self) -> None:
        # Garmin numbers a repeat and its children in one sequence; getting
        # this wrong produces a workout that is subtly out of order.
        spec = spec_from_dict({"name": "Intervals", "blocks": INTERVALS})
        steps = _steps_of(build_workout(spec))

        assert [s["stepOrder"] for s in steps] == [1, 2, 5]
        assert [c["stepOrder"] for c in steps[1]["workoutSteps"]] == [3, 4]

    def test_a_repeat_carries_its_iteration_count(self) -> None:
        spec = spec_from_dict({"name": "Intervals", "blocks": INTERVALS})
        repeat = _steps_of(build_workout(spec))[1]
        assert repeat["numberOfIterations"] == 12
        assert repeat["endCondition"]["conditionTypeKey"] == "iterations"

    def test_pace_targets_are_speeds_with_the_faster_bound_higher(self) -> None:
        """Garmin stores a pace target as a speed range in m/s.

        Inverting the bounds produces a workout it accepts and the watch then
        ignores — a failure with no error message anywhere.
        """
        spec = spec_from_dict({"name": "Intervals", "blocks": INTERVALS})
        interval = _steps_of(build_workout(spec))[1]["workoutSteps"][0]

        assert interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
        slow, fast = interval["targetValueOne"], interval["targetValueTwo"]
        assert fast > slow
        # 3:45 ± 5s is 3:40–3:50, i.e. 4.348–4.545 m/s.
        assert slow == pytest.approx(1000 / 230, abs=0.01)
        assert fast == pytest.approx(1000 / 220, abs=0.01)

    def test_a_step_without_a_target_says_so_explicitly(self) -> None:
        spec = spec_from_dict({"name": "Intervals", "blocks": INTERVALS})
        warmup = _steps_of(build_workout(spec))[0]
        assert warmup["targetType"]["workoutTargetTypeKey"] == "no.target"

    def test_distance_and_time_map_to_the_right_end_condition(self) -> None:
        spec = spec_from_dict(
            {
                "name": "Mixed",
                "blocks": [
                    {"kind": "interval", "distance_m": 2000},
                    {"kind": "recovery", "duration_s": 90},
                ],
            }
        )
        steps = _steps_of(build_workout(spec))
        assert steps[0]["endCondition"]["conditionTypeKey"] == "distance"
        assert steps[0]["endConditionValue"] == 2000
        assert steps[1]["endCondition"]["conditionTypeKey"] == "time"
        assert steps[1]["endConditionValue"] == 90

    def test_the_estimate_is_plausible(self) -> None:
        # 20 min + 12 x 2 min + 10 min = 54 min.
        spec = spec_from_dict({"name": "Intervals", "blocks": INTERVALS})
        assert spec.estimated_seconds == pytest.approx(54 * 60, abs=60)

    def test_the_description_reads_like_a_session(self) -> None:
        # This string is what a human is shown before agreeing to anything, so
        # it has to be legible rather than a dump of fields.
        spec = spec_from_dict({"name": "Intervals", "blocks": INTERVALS})
        assert spec.describe() == (
            "warmup 20min → 12 × (interval 1min at 3:45/km, then recovery 1min) → cooldown 10min"
        )


class TestToolSafety:
    """The gates that stand between a suggestion and the athlete's account."""

    def test_writing_is_off_by_default(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cloning this repository must not hand a model the ability to modify
        # someone's Garmin account.
        assert settings.enable_writes is False
        monkeypatch.setattr(tools, "_settings", lambda: settings)
        result = tools.create_workout("Session", INTERVALS)
        assert result["created"] is False
        assert "disabled" in result["error"]

    def test_a_first_call_previews_and_creates_nothing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enabled = settings.model_copy(update={"enable_writes": True})
        monkeypatch.setattr(tools, "_settings", lambda: enabled)

        def must_not_run(*_a: object, **_k: object) -> None:
            raise AssertionError("a workout was sent without confirmation")

        monkeypatch.setattr("garmin_mcp.ingest.worker.request_workout", must_not_run)

        result = tools.create_workout("Session", INTERVALS)
        assert result["created"] is False
        assert "preview" in result
        assert "confirm=true" in result["next_step"]

    def test_confirming_sends_it(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        enabled = settings.model_copy(update={"enable_writes": True})
        monkeypatch.setattr(tools, "_settings", lambda: enabled)

        sent: list[dict[str, Any]] = []

        def fake_request(payload: dict[str, Any], *_a: object, **_k: object) -> dict[str, Any]:
            sent.append(payload)
            return {
                "created": {
                    "workout_id": 42,
                    "name": payload["spec"]["name"],
                    "url": "https://connect.garmin.com/modern/workout/42",
                },
                "structure": "…",
            }

        monkeypatch.setattr("garmin_mcp.ingest.worker.request_workout", fake_request)

        result = tools.create_workout("Session", INTERVALS, confirm=True)
        assert result["created"] is True
        assert result["workout_id"] == 42
        assert len(sent) == 1

    def test_an_invalid_session_fails_before_anything_is_sent(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enabled = settings.model_copy(update={"enable_writes": True})
        monkeypatch.setattr(tools, "_settings", lambda: enabled)

        def must_not_run(*_a: object, **_k: object) -> None:
            raise AssertionError("an invalid workout reached the network")

        monkeypatch.setattr("garmin_mcp.ingest.worker.request_workout", must_not_run)

        with pytest.raises(ValueError, match="exactly one"):
            tools.create_workout("Broken", [{"kind": "interval"}], confirm=True)

    def test_deleting_also_needs_confirmation(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enabled = settings.model_copy(update={"enable_writes": True})
        monkeypatch.setattr(tools, "_settings", lambda: enabled)
        result = tools.delete_workout(42)
        assert result["deleted"] is False
        assert result["would_delete"] == 42


class TestDiscoverability:
    """An undo nobody can reach is not an undo.

    delete_workout takes an id, and an id was only ever visible in the
    conversation that created the workout. From a fresh session a model could
    create sessions and never remove them — which made the claim that nothing
    here is irreversible true only for whoever had the transcript.
    """

    def test_listing_works_even_when_writing_is_disabled(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reading the library is not a write, and being unable to see what is
        # there is unhelpful regardless of the switch.
        assert settings.enable_writes is False
        monkeypatch.setattr(tools, "_settings", lambda: settings)
        monkeypatch.setattr(
            "garmin_mcp.ingest.worker.request_workout",
            lambda *_a, **_k: {"workouts": [{"workout_id": 7, "name": "Tempo"}]},
        )
        result = tools.list_workouts()
        assert result["count"] == 1
        assert result["workouts"][0]["workout_id"] == 7

    def test_listing_asks_the_worker_rather_than_garmin_directly(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP server must never hold Garmin credentials.

        Reading the workout library needs an authenticated session, so it goes
        through the worker like everything else — otherwise adding this feature
        would have quietly given the server the ability to authenticate.
        """
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(tools, "_settings", lambda: settings)
        monkeypatch.setattr(
            "garmin_mcp.ingest.worker.request_workout",
            lambda payload, *_a, **_k: seen.append(payload) or {"workouts": []},
        )
        tools.list_workouts(limit=5)
        assert seen == [{"action": "list", "limit": 5}]

    def test_deleting_also_goes_through_the_worker(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enabled = settings.model_copy(update={"enable_writes": True})
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(tools, "_settings", lambda: enabled)
        monkeypatch.setattr(
            "garmin_mcp.ingest.worker.request_workout",
            lambda payload, *_a, **_k: seen.append(payload) or {"deleted": 42},
        )
        result = tools.delete_workout(42, confirm=True)
        assert result["deleted"] is True
        assert seen == [{"action": "delete", "workout_id": 42}]

    def test_creation_sends_the_spec_under_a_create_action(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enabled = settings.model_copy(update={"enable_writes": True})
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(tools, "_settings", lambda: enabled)
        monkeypatch.setattr(
            "garmin_mcp.ingest.worker.request_workout",
            lambda payload, *_a, **_k: (
                seen.append(payload) or {"created": {"workout_id": 1, "name": "x", "url": "u"}}
            ),
        )
        tools.create_workout("Session", INTERVALS, confirm=True)
        assert seen[0]["action"] == "create"
        assert seen[0]["spec"]["name"] == "Session"

    def test_the_limit_is_capped(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(tools, "_settings", lambda: settings)
        monkeypatch.setattr(
            "garmin_mcp.ingest.worker.request_workout",
            lambda payload, *_a, **_k: seen.append(payload) or {"workouts": []},
        )
        tools.list_workouts(limit=10**6)
        assert seen[0]["limit"] == 100

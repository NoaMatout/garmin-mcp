"""Describing a structured session, and turning it into Garmin's format.

Garmin's workout payload is a nested DTO with dict-wrapped enums, one-based
step ordering and pace targets expressed as metres per second. Correct, and
nothing anyone should be asked to write by hand — least of all a language
model, which would get the ordering right most of the time and silently wrong
occasionally.

So this module takes a small, obvious description — "warm up 20 minutes, then
12 times (1 minute at 3:45, 1 minute easy), then cool down 5 minutes" — and
does the translation. The spec is deliberately close to how a coach speaks,
because that is the shape a session arrives in.

Building is kept entirely separate from sending. A workout can be constructed,
inspected and shown to a human without any credential being involved, which is
what makes a confirmation step meaningful rather than ceremonial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from garmin_mcp.errors import GarminMcpError

StepKind = Literal["warmup", "interval", "recovery", "cooldown", "rest"]

# Garmin's step type ids, from its workout service.
_STEP_TYPES: dict[str, tuple[int, str]] = {
    "warmup": (1, "warmup"),
    "cooldown": (2, "cooldown"),
    "interval": (3, "interval"),
    "recovery": (4, "recovery"),
    "rest": (5, "rest"),
    "repeat": (6, "repeat"),
}

_END_CONDITIONS: dict[str, tuple[int, str]] = {
    "lap.button": (1, "lap.button"),
    "time": (2, "time"),
    "distance": (3, "distance"),
    "iterations": (7, "iterations"),
    "reps": (10, "reps"),
}

_TARGET_TYPES: dict[str, tuple[int, str]] = {
    "no.target": (1, "no.target"),
    "pace.zone": (6, "pace.zone"),
    "heart.rate.zone": (4, "heart.rate.zone"),
}

_SPORT_TYPES: dict[str, tuple[int, str]] = {
    "running": (1, "running"),
    "cycling": (2, "cycling"),
    # FIXME: swimming's id conflicts with strength's. Reading a strength
    # workout back from Garmin returns sportTypeId 5 for strength_training,
    # so 5 cannot also be swimming — one of the two is wrong, and the API is
    # the authority on strength. Left as-is rather than guessed at: the same
    # trick that settled strength (create one by hand, read it back) will
    # settle swimming, and no swimming workout has ever been created here.
    "swimming": (5, "swimming"),
    "strength": (5, "strength_training"),
}

# Garmin identifies an exercise by a (category, name) pair drawn from a closed
# taxonomy. No endpoint serves that taxonomy — several plausible paths all 404
# — and the library does not ship it, so every pair below was read back from a
# real workout built by hand in Garmin Connect. Guessed pairs are deliberately
# absent: Garmin accepts an unknown name without complaint and the mistake
# only surfaces on the watch, mid-session. Anything missing can still be sent
# by passing "CATEGORY/EXERCISE_NAME" directly.
_EXERCISES: dict[str, tuple[str, str]] = {
    "bench_press": ("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
    "weighted_pull_up": ("PULL_UP", "WEIGHTED_PULL_UP"),
    "deadlift": ("DEADLIFT", "BARBELL_DEADLIFT"),
}

# Warm-up and rest steps carry no exercise; Garmin still wants a category.
_NEUTRAL_CATEGORY = "CARDIO"

# Only used to estimate a duration for the listing. A heavy triple and a set
# of twelve are not the same tempo, but nothing downstream depends on this
# being right, and Garmin recomputes its own estimate anyway.
_SECONDS_PER_REP = 4

_WEIGHT_UNIT: dict[str, Any] = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

# A pace outside this range is a typo, not a session. Roughly 2:00/km — faster
# than the world record — to 20:00/km, slower than walking.
_MIN_PACE_S = 120
_MAX_PACE_S = 1200


class WorkoutSpecError(GarminMcpError):
    """The requested session does not describe something runnable."""


def parse_pace(value: str) -> int:
    """Turn `"3:45"` or `"3:45/km"` into seconds per kilometre.

    Rejects implausible values rather than passing them on: a mistyped target
    ends up on a watch, where it is discovered mid-interval.
    """
    text = value.strip().lower().removesuffix("/km").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise WorkoutSpecError(
            f"pace must look like 3:45 or 3:45/km, got {value!r}",
            hint="minutes and seconds per kilometre",
        )

    seconds = int(match.group(1)) * 60 + int(match.group(2))
    if not _MIN_PACE_S <= seconds <= _MAX_PACE_S:
        raise WorkoutSpecError(f"pace {value!r} is outside the plausible range 2:00–20:00 per km")
    return seconds


def pace_to_mps(seconds_per_km: float) -> float:
    return 1000.0 / seconds_per_km


def format_pace_s(seconds_per_km: float) -> str:
    total = round(seconds_per_km)
    return f"{total // 60}:{total % 60:02d}/km"


@dataclass(slots=True)
class Step:
    """One block of a session.

    Exactly one of `duration_s`, `distance_m` or `reps` must be set — that is
    the condition ending the step. A step with none would run until the athlete
    pressed lap, which is never what a written session means.

    `reps` is what makes a strength set: the watch shows the exercise and the
    load, and the athlete closes each set with the lap button.
    """

    kind: StepKind
    duration_s: int | None = None
    distance_m: float | None = None
    reps: int | None = None
    # Either a key of _EXERCISES or a raw "CATEGORY/EXERCISE_NAME" pair.
    exercise: str | None = None
    weight_kg: float | None = None
    target_pace: str | None = None
    pace_tolerance_s: int = 5
    # Whether the watch should police the target. Off means the pace is
    # written into the step so it is visible while running, but no alert
    # fires — some athletes want the number, not the beeping, and a watch
    # buzzing on every fluctuation trains you to ignore it.
    alert: bool = True
    note: str | None = None

    def validate(self) -> None:
        if self.kind not in _STEP_TYPES:
            raise WorkoutSpecError(
                f"unknown step kind {self.kind!r}; expected one of "
                f"{sorted(k for k in _STEP_TYPES if k != 'repeat')}"
            )
        given = [
            name
            for name, value in (
                ("duration_s", self.duration_s),
                ("distance_m", self.distance_m),
                ("reps", self.reps),
            )
            if value is not None
        ]
        if len(given) != 1:
            raise WorkoutSpecError(
                f"step {self.kind!r} needs exactly one of duration_s, distance_m or reps"
                + (f", got {sorted(given)}" if given else "")
            )
        if self.duration_s is not None and self.duration_s <= 0:
            raise WorkoutSpecError("duration_s must be positive")
        if self.distance_m is not None and self.distance_m <= 0:
            raise WorkoutSpecError("distance_m must be positive")
        if self.reps is not None:
            if self.reps <= 0:
                raise WorkoutSpecError("reps must be positive")
            # Without an exercise the watch shows a bare rep count and no
            # movement, which is not a session anyone can follow.
            if not self.exercise:
                raise WorkoutSpecError("a step counted in reps needs an exercise")
            resolve_exercise(self.exercise)
        if self.weight_kg is not None:
            if self.reps is None:
                raise WorkoutSpecError("weight_kg only means anything on a step counted in reps")
            if self.weight_kg < 0:
                raise WorkoutSpecError("weight_kg cannot be negative")
        if self.target_pace is not None:
            parse_pace(self.target_pace)

    @property
    def estimated_seconds(self) -> int:
        """How long this step takes, for the workout's duration estimate."""
        if self.duration_s is not None:
            return self.duration_s
        if self.reps is not None:
            return self.reps * _SECONDS_PER_REP
        pace = parse_pace(self.target_pace) if self.target_pace else 300
        return round((self.distance_m or 0) / 1000 * pace)

    def describe(self) -> str:
        if self.reps is not None:
            load = f" @ {self.weight_kg:g} kg" if self.weight_kg else ""
            return f"{self.kind} {self.reps} × {self.exercise}{load}"
        if self.duration_s is not None:
            extent = _human_duration(self.duration_s)
        else:
            metres = self.distance_m or 0
            extent = f"{metres / 1000:g} km" if metres >= 1000 else f"{metres:g} m"
        target = ""
        if self.target_pace:
            # Say which it is either way. Saying nothing when alerts are on
            # meant an athlete could confirm a session without ever seeing
            # that their watch would beep through every interval.
            state = "alerts on" if self.alert else "no alert"
            target = f" at {self.target_pace}/km ({state})"
        return f"{self.kind} {extent}{target}"


@dataclass(slots=True)
class Repeat:
    """A block repeated N times — the shape of every interval session."""

    times: int
    steps: list[Step]

    def validate(self) -> None:
        if self.times < 1:
            raise WorkoutSpecError("a repeat needs at least one iteration")
        if self.times > 99:
            raise WorkoutSpecError("Garmin allows at most 99 repetitions")
        if not self.steps:
            raise WorkoutSpecError("a repeat needs at least one step")
        for step in self.steps:
            step.validate()

    @property
    def estimated_seconds(self) -> int:
        return self.times * sum(s.estimated_seconds for s in self.steps)

    def describe(self) -> str:
        inner = ", then ".join(s.describe() for s in self.steps)
        return f"{self.times} × ({inner})"


@dataclass(slots=True)
class WorkoutSpec:
    """A complete session, in the terms a coach would use."""

    name: str
    blocks: list[Step | Repeat] = field(default_factory=list)
    sport: str = "running"
    description: str | None = None

    def validate(self) -> None:
        if not self.name.strip():
            raise WorkoutSpecError("the workout needs a name")
        if len(self.name) > 80:
            raise WorkoutSpecError("workout names are limited to 80 characters")
        if self.sport not in _SPORT_TYPES:
            raise WorkoutSpecError(
                f"unsupported sport {self.sport!r}; expected one of {sorted(_SPORT_TYPES)}"
            )
        if not self.blocks:
            raise WorkoutSpecError("the workout has no steps")
        for block in self.blocks:
            block.validate()

    @property
    def estimated_seconds(self) -> int:
        return sum(b.estimated_seconds for b in self.blocks)

    def describe(self) -> str:
        """A one-line rendering, for showing a human before anything is sent."""
        return " → ".join(b.describe() for b in self.blocks)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sport": self.sport,
            "structure": self.describe(),
            "estimated_duration": _human_duration(self.estimated_seconds),
            "steps": [b.describe() for b in self.blocks],
        }


def resolve_exercise(name: str) -> tuple[str, str]:
    """Turn an exercise name into Garmin's (category, exerciseName) pair.

    Accepts a friendly key from `_EXERCISES`, or a raw `"CATEGORY/NAME"` pair
    for anything not yet confirmed against the real API. The raw form is an
    escape hatch on purpose — the taxonomy is closed and undocumented, and
    being able to send an unverified pair beats being unable to program an
    exercise at all.
    """
    key = name.strip()
    if "/" in key:
        category, _, exercise = key.partition("/")
        if not category.strip() or not exercise.strip():
            raise WorkoutSpecError(
                f"raw exercise {name!r} must look like CATEGORY/EXERCISE_NAME",
                hint="for example BENCH_PRESS/BARBELL_BENCH_PRESS",
            )
        return category.strip().upper(), exercise.strip().upper()

    try:
        return _EXERCISES[key.lower()]
    except KeyError:
        raise WorkoutSpecError(
            f"unknown exercise {name!r}; confirmed names are {sorted(_EXERCISES)}",
            hint="or pass a raw CATEGORY/EXERCISE_NAME pair",
        ) from None


def _silent_target_note(step: Step) -> str | None:
    """Text form of a target the watch will not alert on."""
    if step.target_pace and not step.alert:
        return f"target {step.target_pace}/km"
    return None


def _human_duration(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}"
    return f"{minutes}min" if not secs else f"{minutes}min{secs:02d}"


# Garmin names the key field differently per enum; an explicit mapping beats
# deriving it from the id field name.
_KEY_FIELDS = {
    "stepTypeId": "stepTypeKey",
    "conditionTypeId": "conditionTypeKey",
    "workoutTargetTypeId": "workoutTargetTypeKey",
    "sportTypeId": "sportTypeKey",
}


def _enum(table: dict[str, tuple[int, str]], key: str, id_field: str) -> dict[str, Any]:
    """Garmin wraps every enum as {someTypeId, someTypeKey}."""
    number, name = table[key]
    return {id_field: number, _KEY_FIELDS[id_field]: name}


def _build_step(step: Step, order: int) -> Any:
    from garminconnect.workout import ExecutableStep

    payload: dict[str, Any] = {
        "stepOrder": order,
        "stepType": _enum(_STEP_TYPES, step.kind, "stepTypeId"),
    }

    if step.duration_s is not None:
        payload["endCondition"] = _enum(_END_CONDITIONS, "time", "conditionTypeId")
        payload["endConditionValue"] = float(step.duration_s)
    elif step.reps is not None:
        payload["endCondition"] = _enum(_END_CONDITIONS, "reps", "conditionTypeId")
        payload["endConditionValue"] = float(step.reps)
        category, exercise = resolve_exercise(step.exercise or "")
        payload["category"] = category
        payload["exerciseName"] = exercise
        # Garmin wants the unit alongside the number even when there is no
        # load — a bodyweight set is weightValue 0, not a missing field.
        payload["weightValue"] = float(step.weight_kg or 0)
        payload["weightUnit"] = dict(_WEIGHT_UNIT)
    else:
        payload["endCondition"] = _enum(_END_CONDITIONS, "distance", "conditionTypeId")
        payload["endConditionValue"] = float(step.distance_m or 0)

    if step.target_pace and step.alert:
        # Garmin expresses a pace target as a speed range in metres per second,
        # so the faster pace is the *higher* value. Getting these the wrong way
        # round produces a workout the watch silently ignores.
        centre = parse_pace(step.target_pace)
        fast = pace_to_mps(centre - step.pace_tolerance_s)
        slow = pace_to_mps(centre + step.pace_tolerance_s)
        payload["targetType"] = _enum(_TARGET_TYPES, "pace.zone", "workoutTargetTypeId")
        payload["targetValueOne"] = round(slow, 4)
        payload["targetValueTwo"] = round(fast, 4)
    else:
        payload["targetType"] = _enum(_TARGET_TYPES, "no.target", "workoutTargetTypeId")

    # An unalerted target still belongs on the screen — the athlete is pacing
    # off it themselves, which is the entire point of switching alerts off.
    parts = [p for p in (step.note, _silent_target_note(step)) if p]
    if parts:
        payload["description"] = " — ".join(parts)

    return ExecutableStep(**payload)


def _build_repeat(repeat: Repeat, order: int) -> Any:
    from garminconnect.workout import RepeatGroup

    children = [_build_step(step, order + 1 + i) for i, step in enumerate(repeat.steps)]
    return RepeatGroup(
        stepOrder=order,
        stepType=_enum(_STEP_TYPES, "repeat", "stepTypeId"),
        numberOfIterations=repeat.times,
        workoutSteps=children,
        endCondition=_enum(_END_CONDITIONS, "iterations", "conditionTypeId"),
        endConditionValue=float(repeat.times),
    )


def build_workout(spec: WorkoutSpec) -> Any:
    """Turn a spec into the typed object garminconnect uploads.

    Validation happens first and refuses anything malformed, because the
    failure mode on the other side is a session that reaches the watch and
    behaves oddly halfway through a hard interval.
    """
    spec.validate()

    from garminconnect.workout import (
        CyclingWorkout,
        RunningWorkout,
        StrengthWorkout,
        SwimmingWorkout,
        WorkoutSegment,
    )

    steps: list[Any] = []
    order = 1
    for block in spec.blocks:
        if isinstance(block, Repeat):
            steps.append(_build_repeat(block, order))
            # A repeat consumes an order slot for itself plus one per child.
            order += 1 + len(block.steps)
        else:
            steps.append(_build_step(block, order))
            order += 1

    sport = _enum(_SPORT_TYPES, spec.sport, "sportTypeId")
    segment = WorkoutSegment(segmentOrder=1, sportType=sport, workoutSteps=steps)

    model = {
        "running": RunningWorkout,
        "cycling": CyclingWorkout,
        "swimming": SwimmingWorkout,
        "strength": StrengthWorkout,
    }[spec.sport]

    return model(
        workoutName=spec.name.strip(),
        sportType=sport,
        estimatedDurationInSecs=spec.estimated_seconds,
        workoutSegments=[segment],
        author={},
        description=spec.description,
    )


def spec_from_dict(payload: dict[str, Any]) -> WorkoutSpec:
    """Build a spec from plain JSON, as an MCP tool receives it.

    Kept tolerant about shape and strict about meaning: unknown keys are an
    error rather than being ignored, so a mistyped `distance` never becomes a
    step that silently runs on the lap button.
    """
    blocks: list[Step | Repeat] = []
    for raw in payload.get("blocks", []):
        blocks.append(_block_from_dict(raw))

    return WorkoutSpec(
        name=str(payload.get("name", "")),
        blocks=blocks,
        sport=str(payload.get("sport", "running")),
        description=payload.get("description"),
    )


_STEP_KEYS = {
    "kind",
    "duration_s",
    "distance_m",
    "reps",
    "exercise",
    "weight_kg",
    "target_pace",
    "pace_tolerance_s",
    "alert",
    "note",
}
_REPEAT_KEYS = {"repeat", "times", "steps"}


def _block_from_dict(raw: dict[str, Any]) -> Step | Repeat:
    if "times" in raw or "steps" in raw or raw.get("repeat"):
        unknown = set(raw) - _REPEAT_KEYS
        if unknown:
            raise WorkoutSpecError(f"unknown keys in repeat block: {sorted(unknown)}")
        return Repeat(
            times=int(raw.get("times", 0)),
            steps=[_step_from_dict(s) for s in raw.get("steps", [])],
        )
    return _step_from_dict(raw)


def _step_from_dict(raw: dict[str, Any]) -> Step:
    unknown = set(raw) - _STEP_KEYS
    if unknown:
        raise WorkoutSpecError(
            f"unknown keys in step: {sorted(unknown)}; expected {sorted(_STEP_KEYS)}"
        )
    return Step(
        kind=raw.get("kind", "interval"),
        duration_s=raw.get("duration_s"),
        distance_m=raw.get("distance_m"),
        reps=raw.get("reps"),
        exercise=raw.get("exercise"),
        weight_kg=raw.get("weight_kg"),
        target_pace=raw.get("target_pace"),
        pace_tolerance_s=int(raw.get("pace_tolerance_s", 5)),
        alert=bool(raw.get("alert", True)),
        note=raw.get("note"),
    )

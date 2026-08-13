"""Sending a workout to Garmin.

Kept apart from `ActivitySource` on purpose. That interface is read-only and
its implementations cannot authenticate; writing is a different capability
with a different risk profile, and blurring them would quietly turn every
backend into something that can modify the athlete's account.

Three properties this module is built around:

* **Off unless asked.** Writing requires `GARMIN_ENABLE_WRITES=true`. Someone
  cloning this repository should not inherit a server that can push sessions
  to their watch because a model decided it was a good idea.
* **Reversible.** Creating returns the workout id, and `delete_workout` undoes
  it. Nothing here is a one-way door.
* **Not scheduled or pushed by default.** Creating a workout puts it in the
  athlete's library. Scheduling it on a date, or pushing it onto a device, are
  separate opt-in steps — a suggestion appearing on tomorrow's calendar
  unasked is a different thing from a suggestion sitting in a list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.errors import GarminAuthError, GarminError, GarminMcpError
from garmin_mcp.garmin.workouts import WorkoutSpec, build_workout
from garmin_mcp.logging import get_logger

log = get_logger(__name__)


class WritesDisabledError(GarminMcpError):
    """Writing to Garmin is switched off."""

    def __init__(self) -> None:
        super().__init__(
            "writing to Garmin is disabled",
            hint="set GARMIN_ENABLE_WRITES=true in .env and restart the ingest "
            "worker to allow creating workouts",
        )


@dataclass(frozen=True, slots=True)
class CreatedWorkout:
    workout_id: int
    name: str
    url: str

    def as_dict(self) -> dict[str, Any]:
        return {"workout_id": self.workout_id, "name": self.name, "url": self.url}


def _client(settings: Settings) -> Any:
    """Resume the saved session. Same rule as everywhere: never a fresh login.

    A write path that could prompt for credentials would hang the worker, and
    the worker is exactly where this runs.
    """
    import garminconnect

    token_dir = settings.token_dir
    if not token_dir.is_dir() or not any(token_dir.iterdir()):
        raise GarminAuthError(f"no saved Garmin session in {token_dir}")

    client = garminconnect.Garmin()
    try:
        client.login(str(token_dir))
    except Exception as exc:
        from garmin_mcp.garmin.cffi_source import _translate

        raise _translate(exc) from exc
    return client


def create_workout(
    spec: WorkoutSpec,
    settings: Settings | None = None,
) -> CreatedWorkout:
    """Upload a session to the athlete's Garmin workout library.

    Validation runs before anything leaves the machine, so a malformed session
    fails here rather than arriving on a watch and behaving oddly in the middle
    of a hard interval.
    """
    settings = settings or get_settings()
    if not settings.enable_writes:
        raise WritesDisabledError()

    payload = build_workout(spec)  # validates as a side effect
    client = _client(settings)

    upload = {
        "running": "upload_running_workout",
        "cycling": "upload_cycling_workout",
        "swimming": "upload_swimming_workout",
    }[spec.sport]

    try:
        response = getattr(client, upload)(payload)
    except Exception as exc:
        from garmin_mcp.garmin.cffi_source import _translate

        raise _translate(exc) from exc

    workout_id = _extract_id(response)
    if workout_id is None:
        raise GarminError(f"Garmin accepted the workout but returned no id: {response!r}")

    log.info("workout.created", workout_id=workout_id, name=spec.name, sport=spec.sport)
    return CreatedWorkout(
        workout_id=workout_id,
        name=spec.name,
        url=f"https://connect.garmin.com/modern/workout/{workout_id}",
    )


def delete_workout(workout_id: int, settings: Settings | None = None) -> None:
    """Remove a workout. The undo for `create_workout`."""
    settings = settings or get_settings()
    if not settings.enable_writes:
        raise WritesDisabledError()

    client = _client(settings)
    try:
        client.delete_workout(str(workout_id))
    except Exception as exc:
        from garmin_mcp.garmin.cffi_source import _translate

        raise _translate(exc) from exc
    log.info("workout.deleted", workout_id=workout_id)


def schedule_workout(
    workout_id: int,
    date_str: str,
    settings: Settings | None = None,
) -> None:
    """Put an existing workout on a date in the athlete's calendar."""
    settings = settings or get_settings()
    if not settings.enable_writes:
        raise WritesDisabledError()

    client = _client(settings)
    try:
        client.schedule_workout(str(workout_id), date_str)
    except Exception as exc:
        from garmin_mcp.garmin.cffi_source import _translate

        raise _translate(exc) from exc
    log.info("workout.scheduled", workout_id=workout_id, date=date_str)


def _extract_id(response: Any) -> int | None:
    """Find the workout id in whatever shape Garmin replied with."""
    if isinstance(response, int):
        return response
    if isinstance(response, dict):
        for key in ("workoutId", "id", "workoutid"):
            value = response.get(key)
            if isinstance(value, int | str) and str(value).isdigit():
                return int(value)
    return None

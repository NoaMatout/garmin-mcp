"""Turning database rows into something worth spending tokens on.

Every byte returned by a tool lands in a language model's context, so the
formatting rules here are not cosmetic:

* **Nulls are dropped.** An indoor run has no GPS, no temperature and no
  power; emitting fourteen `null`s per activity across twenty activities is
  hundreds of wasted tokens that also invite the model to comment on missing
  data nobody asked about.
* **Units are resolved, not implied.** `"4:42/km"` is unambiguous and costs
  less than `avg_speed_mps: 3.5432` plus the arithmetic to interpret it. Raw
  values are kept alongside only where a computation might follow.
* **Pace or speed, not both.** Runners read minutes per kilometre, cyclists
  read km/h. Emitting both doubles the number and halves the clarity.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from garmin_mcp.domain.units import (
    format_duration,
    format_pace,
    is_cycling,
    is_running,
)


def _round(value: Any, digits: int = 1) -> Any:
    return round(value, digits) if isinstance(value, int | float) else value


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None, empty or zero-length."""
    return {k: v for k, v in data.items() if v is not None and v != ""}


def _local_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return None


def speed_or_pace(row: dict[str, Any]) -> dict[str, Any]:
    """Express velocity the way an athlete in that sport would read it."""
    speed = row.get("avg_speed_mps")
    if not speed or speed <= 0:
        return {}

    sport = row.get("sport")
    if is_cycling(sport):
        return {"avg_speed_kmh": round(speed * 3.6, 1)}
    if is_running(sport) or sport in ("swimming", "transition", None):
        return {"avg_pace": format_pace(1000.0 / speed)}
    return {"avg_speed_kmh": round(speed * 3.6, 1)}


def summarize_activity(row: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    """One activity as a compact record.

    `verbose` adds the fields worth seeing when an activity is the subject of
    the question rather than one line in a list.
    """
    summary: dict[str, Any] = {
        "activity_id": row.get("activity_id"),
        "date": _local_date(row.get("start_time_local")),
        "sport": row.get("sport"),
        "name": row.get("name"),
        "distance_km": _round((row.get("total_distance_m") or 0) / 1000, 2) or None,
        "duration": format_duration(row.get("total_timer_time_s")),
        **speed_or_pace(row),
        "avg_hr": row.get("avg_heart_rate"),
        "ascent_m": _round(row.get("total_ascent_m"), 0),
    }

    if row.get("sub_sport") and row["sub_sport"] not in ("generic", row.get("sport")):
        summary["sub_sport"] = row["sub_sport"]

    # Flag legs so the model never presents one as a standalone session.
    if row.get("parent_activity_id"):
        summary["part_of_activity"] = row["parent_activity_id"]

    if verbose:
        summary |= {
            "max_hr": row.get("max_heart_rate"),
            "avg_cadence": _round(row.get("avg_cadence"), 0),
            "avg_power_w": _round(row.get("avg_power_w"), 0),
            "elapsed": format_duration(row.get("total_elapsed_time_s")),
            "calories": row.get("total_calories"),
            "training_effect": _round(row.get("aerobic_training_effect"), 1),
            "descent_m": _round(row.get("total_descent_m"), 0),
            "avg_temperature_c": _round(row.get("avg_temperature_c"), 0),
            "device": row.get("device_product"),
        }

    return _compact(summary)


def summarize_lap(lap: dict[str, Any], sport: str | None) -> dict[str, Any]:
    """One lap, in the shape that makes an interval session readable."""
    speed = lap.get("avg_speed_mps")
    velocity: dict[str, Any] = {}
    if speed and speed > 0:
        velocity = (
            {"speed_kmh": round(speed * 3.6, 1)}
            if is_cycling(sport)
            else {"pace": format_pace(1000.0 / speed)}
        )

    return _compact(
        {
            "lap": (lap.get("lap_index") or 0) + 1,  # humans count from one
            "distance_km": _round((lap.get("total_distance_m") or 0) / 1000, 2) or None,
            "duration": format_duration(lap.get("total_timer_time_s")),
            **velocity,
            "avg_hr": lap.get("avg_heart_rate"),
            "max_hr": lap.get("max_heart_rate"),
            "avg_power_w": _round(lap.get("avg_power_w"), 0),
            "avg_cadence": _round(lap.get("avg_cadence"), 0),
            "intensity": lap.get("intensity") if lap.get("intensity") != "active" else None,
        }
    )


def summarize_week(rows: list[dict[str, Any]], week_start: date) -> dict[str, Any]:
    """Per-sport volume for one week, plus a combined line.

    Volume only — time, distance, ascent. Garmin's Training Effect is a 1-to-5
    score describing one session; adding those up across a week produces a
    number that looks authoritative and means nothing, and a training log is
    exactly where a fabricated metric does damage, because it reads as an
    answer.

    Zero distance is dropped rather than shown. A strength session has no
    distance, and `0.0 km` is noise that invites commentary on data nobody
    asked about — the same rule the per-activity formatter already applies.
    """
    by_sport = [
        _compact(
            {
                "sport": row.get("sport"),
                "activities": row.get("activities"),
                "distance_km": _round(row.get("distance_km"), 1) or None,
                "moving_time": format_duration(row.get("moving_time_s")),
                "ascent_m": _round(row.get("ascent_m"), 0) or None,
                "avg_hr": _round(row.get("avg_heart_rate"), 0),
            }
        )
        for row in rows
    ]

    return _compact(
        {
            "week_start": week_start.isoformat(),
            "week_end": _iso_week_end(week_start),
            "totals": _compact(
                {
                    "activities": sum(r.get("activities") or 0 for r in rows),
                    "distance_km": _round(sum(r.get("distance_km") or 0 for r in rows), 1) or None,
                    "moving_time": format_duration(sum(r.get("moving_time_s") or 0 for r in rows)),
                    "ascent_m": _round(sum(r.get("ascent_m") or 0 for r in rows), 0) or None,
                }
            ),
            "by_sport": by_sport,
        }
    )


def _iso_week_end(week_start: date) -> str:
    from datetime import timedelta

    return (week_start + timedelta(days=6)).isoformat()


def _delta(a: Any, b: Any, digits: int = 1) -> Any:
    if not isinstance(a, int | float) or not isinstance(b, int | float):
        return None
    return round(b - a, digits)


def compare(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    """Two activities side by side, with the differences precomputed.

    The deltas are the point. Asked to compare, a model given only two columns
    of numbers will do the subtraction itself and occasionally get it wrong;
    doing it in Python removes that failure mode entirely.

    Sign convention: positive means the second activity is greater. For pace
    that reads backwards — a positive delta is slower — so it is labelled.
    """
    a_pace = _pace_seconds(left)
    b_pace = _pace_seconds(right)

    deltas = _compact(
        {
            "distance_km": _delta(
                (left.get("total_distance_m") or 0) / 1000,
                (right.get("total_distance_m") or 0) / 1000,
                2,
            ),
            "duration_s": _delta(
                left.get("total_timer_time_s"), right.get("total_timer_time_s"), 0
            ),
            "avg_hr": _delta(left.get("avg_heart_rate"), right.get("avg_heart_rate"), 0),
            "ascent_m": _delta(left.get("total_ascent_m"), right.get("total_ascent_m"), 0),
            "avg_power_w": _delta(left.get("avg_power_w"), right.get("avg_power_w"), 0),
        }
    )

    if a_pace and b_pace:
        difference = round(b_pace - a_pace)
        deltas["pace_s_per_km"] = difference
        deltas["pace_verdict"] = (
            "second is faster"
            if difference < 0
            else "second is slower"
            if difference > 0
            else "identical"
        )

    return {
        "a": summarize_activity(left, verbose=True),
        "b": summarize_activity(right, verbose=True),
        "delta_b_minus_a": deltas,
        "note": "positive values mean B is greater than A",
    }


def _pace_seconds(row: dict[str, Any]) -> float | None:
    speed = row.get("avg_speed_mps")
    return 1000.0 / speed if speed and speed > 0 else None


def format_streams(
    streams: dict[str, list[Any]],
    *,
    activity_id: int,
    total_samples: int,
    returned: int,
    extrema: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Wrap the columnar series with the context needed to read them.

    `extrema` carries the true peaks over every raw sample, and it is not
    decoration. The series is averaged, and averaging flattens extremes: on a
    real ride, a 10-point overview reported a maximum heart rate of 160 against
    an actual 174. Anyone reading only the series will state that wrong number
    confidently, so the honest one travels alongside it.
    """
    rounded = {
        name: [round(v, 4) if isinstance(v, float) else v for v in values]
        for name, values in streams.items()
    }
    payload: dict[str, Any] = {
        "activity_id": activity_id,
        "points": returned,
        "source_samples": total_samples,
        "series": rounded,
    }

    if extrema:
        payload["true_range"] = extrema

    if returned < total_samples:
        payload["downsampled"] = (
            f"averaged {total_samples} samples into {returned} points. "
            "The series is smoothed, so its highest and lowest values understate "
            "the real peaks — use true_range for actual minima and maxima, and "
            "raise max_points for finer shape."
        )
    return payload

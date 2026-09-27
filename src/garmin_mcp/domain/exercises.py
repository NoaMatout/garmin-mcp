"""Garmin's exercise taxonomy.

Two layers, and the difference matters when programming a session.

The **category** — `bench_press`, `row`, `triceps_extension` — is a closed
list that ships inside the FIT profile, so it can be read rather than
guessed. The **name** below it — `BARBELL_BENCH_PRESS` under `BENCH_PRESS`
— is not published anywhere reachable: no endpoint serves it and the
library does not carry it, so the only confirmed pairs are the ones read
back from a workout built by hand in Garmin Connect.

Garmin accepts a step carrying a category with an empty name, which is what
makes the accessory half of a session programmable at all: a face pull goes
out as `SHOULDER_STABILITY` with no variant, which is true rather than a
guess that only fails on the watch, mid-session.
"""

from __future__ import annotations

from garmin_mcp.logging import get_logger

log = get_logger(__name__)


def _load_categories() -> dict[int, str]:
    """The FIT profile's exercise_category table, read from fitdecode.

    Guarded: a profile reshuffle upstream must degrade to raw numbers and
    unprogrammable accessories, not stop every strength file from parsing.
    """
    try:
        from fitdecode.profile import FIELD_TYPES

        return {int(k): str(v) for k, v in FIELD_TYPES["exercise_category"].enum.items()}
    except Exception:  # pragma: no cover — depends on the installed profile
        log.warning("fit.exercise_categories_unavailable")
        return {}


CATEGORIES: dict[int, str] = _load_categories()

CATEGORY_NAMES: frozenset[str] = frozenset(CATEGORIES.values())

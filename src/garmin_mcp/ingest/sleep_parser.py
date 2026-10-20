"""Mapping Garmin's daily-sleep JSON onto `SleepSession`.

Reads defensively, the same way `garmin.cffi_source._to_stub` reads an
activity listing entry: this is an undocumented endpoint reached through a
reverse-engineered client, and Garmin has changed field names before without
warning.

Field names were first taken from `garminconnect`'s own typed response model
(`garminconnect.typed.DailySleepDTO`) and turned out to be wrong for two of
them against a real account: HRV and respiration are not where that model
says. Confirmed instead by dumping an actual response's `extra` JSON —
respiration is `averageRespirationValue` inside `dailySleepDTO` (the typed
model has `avgRespirationValue`, missing the "e"), and HRV lives at the
**top level** of the response as `avgOvernightHrv`, not inside `dailySleepDTO`
as `avgSleepHRV` at all. `avgSpO2` genuinely is absent for every device that
lacks a pulse-ox sensor — that one null is real, not a mapping bug.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from garmin_mcp.domain.models import SleepSession


def _epoch_ms(value: Any) -> datetime | None:
    """Garmin's sleep timestamps are milliseconds since the epoch, not ISO text."""
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def parse_sleep(raw: dict[str, Any] | None, calendar_date: date) -> SleepSession | None:
    """One night's sleep, or `None` when Garmin has nothing for this date.

    A day with no watch-detected sleep still returns a `dailySleepDTO` shell
    with every measurement null; `sleepTimeSeconds` absent or zero is what
    distinguishes "no data" from "zero sleep", which never legitimately
    happens for a night the watch actually tracked.
    """
    dto = (raw or {}).get("dailySleepDTO") or {}
    total = dto.get("sleepTimeSeconds")
    if not total:
        return None

    overall = ((dto.get("sleepScores") or {}).get("overall")) or {}
    start_local = _epoch_ms(dto.get("sleepStartTimestampLocal"))
    end_local = _epoch_ms(dto.get("sleepEndTimestampLocal"))

    return SleepSession(
        calendar_date=calendar_date,
        sleep_start_utc=_epoch_ms(dto.get("sleepStartTimestampGMT")),
        sleep_end_utc=_epoch_ms(dto.get("sleepEndTimestampGMT")),
        # Local timestamps arrive already shifted; stored naive like every
        # other *_local column (see schema.sql's header note on the pattern).
        sleep_start_local=start_local.replace(tzinfo=None) if start_local else None,
        sleep_end_local=end_local.replace(tzinfo=None) if end_local else None,
        total_sleep_s=_float(total),
        nap_s=_float(dto.get("napTimeSeconds")),
        deep_sleep_s=_float(dto.get("deepSleepSeconds")),
        light_sleep_s=_float(dto.get("lightSleepSeconds")),
        rem_sleep_s=_float(dto.get("remSleepSeconds")),
        awake_s=_float(dto.get("awakeSleepSeconds")),
        avg_hrv=_float((raw or {}).get("avgOvernightHrv")),
        avg_spo2=_float(dto.get("avgSpO2")),
        avg_respiration=_float(dto.get("averageRespirationValue")),
        lowest_respiration=_float(dto.get("lowestRespirationValue")),
        highest_respiration=_float(dto.get("highestRespirationValue")),
        sleep_score=overall.get("value"),
        sleep_quality=overall.get("qualifierKey"),
        hrv_status=(raw or {}).get("hrvStatus"),
        resting_heart_rate=_int((raw or {}).get("restingHeartRate")),
        body_battery_change=_int((raw or {}).get("bodyBatteryChange")),
        # Kept whole, per-minute arrays included: they are what the `extra
        # JSON absorbs rare fields` convention (schema.sql) exists for, and
        # at one row per night the storage cost is nothing.
        extra=raw or {},
    )

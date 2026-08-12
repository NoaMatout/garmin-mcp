"""Tests for FIT parsing.

Grouped by the thing that would actually break in production: unit
conversions, the multisport split, missing sensor data, and malformed files.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from garmin_mcp.domain.models import is_synthetic_id
from garmin_mcp.errors import FitParseError
from garmin_mcp.ingest.fit_parser import hash_fit_file, parse_fit
from tests import fit_builder


class TestSingleSession:
    """A plain run — 95% of real files."""

    def test_yields_exactly_one_activity(self, run_fit: Path) -> None:
        parsed = parse_fit(run_fit)
        assert parsed.num_sessions == 1
        assert len(parsed.activities) == 1
        assert not parsed.is_multisport
        assert parsed.parent is None

    def test_reads_sport_and_device(self, run_fit: Path) -> None:
        activity = parse_fit(run_fit).activities[0]
        assert activity.sport == "running"
        assert activity.sub_sport == "road"
        assert activity.device_product == "fr955"
        assert activity.device_serial == 3987654321

    def test_summary_metrics(self, run_fit: Path) -> None:
        activity = parse_fit(run_fit).activities[0]
        assert activity.total_distance_m == pytest.approx(6000.0)
        assert activity.total_timer_time_s == pytest.approx(1800.0)
        assert activity.avg_heart_rate == 152
        assert activity.max_heart_rate == 171
        assert activity.total_ascent_m == pytest.approx(42.0)
        assert activity.aerobic_training_effect == pytest.approx(3.4)

    def test_laps_and_records_are_attached(self, run_fit: Path) -> None:
        activity = parse_fit(run_fit).activities[0]
        assert len(activity.laps) == 2
        assert len(activity.records) == 60
        assert activity.laps[0].intensity == "active"
        assert activity.laps[0].lap_trigger == "distance"

    def test_records_carry_their_lap_index(self, run_fit: Path) -> None:
        activity = parse_fit(run_fit).activities[0]
        assert {r.lap_index for r in activity.records} == {0, 1}

    def test_elapsed_seconds_start_at_zero_and_increase(self, run_fit: Path) -> None:
        records = parse_fit(run_fit).activities[0].records
        assert records[0].elapsed_s == 0.0
        assert all(b.elapsed_s > a.elapsed_s for a, b in pairwise(records))


class TestUnitConversion:
    """The conversions that silently corrupt data when wrong."""

    def test_positions_are_converted_from_semicircles(self, run_fit: Path) -> None:
        activity = parse_fit(run_fit).activities[0]
        # Encoded as 45.75 / 4.85 degrees; semicircles round-trip lossily.
        assert activity.start_lat == pytest.approx(45.75, abs=1e-5)
        assert activity.start_lon == pytest.approx(4.85, abs=1e-5)
        assert activity.records[0].lat == pytest.approx(45.75, abs=1e-5)

    def test_running_cadence_is_doubled_to_steps_per_minute(self, run_fit: Path) -> None:
        # FIT stores strides for one leg; humans count both.
        activity = parse_fit(run_fit).activities[0]
        assert activity.avg_cadence == pytest.approx(172.0)
        assert activity.max_cadence == pytest.approx(184.0)

    def test_cycling_cadence_is_left_alone(self, tmp_path: Path) -> None:
        # A doubled 80 rpm would read as an implausible 160 rpm.
        parsed = parse_fit(_write(tmp_path, "tri.fit", fit_builder.build_triathlon()))
        bike = _leg(parsed, "cycling")
        assert bike.avg_cadence is None or bike.avg_cadence <= 120

    def test_local_time_offset_is_derived_from_the_activity_message(self, run_fit: Path) -> None:
        activity = parse_fit(run_fit).activities[0]
        assert activity.tz_offset_seconds == 7200
        assert activity.start_time_utc.hour == 7
        assert activity.start_time_local.hour == 9
        # The local stamp must be naive: it is wall-clock, not an instant.
        assert activity.start_time_local.tzinfo is None


class TestMultisport:
    """A triathlon is one file holding five sessions."""

    def test_splits_into_parent_plus_one_child_per_leg(self, triathlon_fit: Path) -> None:
        parsed = parse_fit(triathlon_fit)
        assert parsed.is_multisport
        assert parsed.num_sessions == 5
        assert len(parsed.activities) == 6  # parent + 5 legs

    def test_transitions_are_preserved_as_sessions(self, triathlon_fit: Path) -> None:
        sports = [a.sport for a in parse_fit(triathlon_fit).activities]
        assert sports == [
            "multisport",
            "swimming",
            "transition",
            "cycling",
            "transition",
            "running",
        ]

    def test_children_point_at_the_parent(self, triathlon_fit: Path) -> None:
        parsed = parse_fit(triathlon_fit)
        parent = parsed.parent
        assert parent is not None
        children = [a for a in parsed.activities if a is not parent]
        assert all(c.parent_activity_id == parent.activity_id for c in children)

    def test_parent_takes_the_garmin_id_and_children_get_synthetic_ones(
        self, triathlon_fit: Path
    ) -> None:
        parsed = parse_fit(triathlon_fit, source="garmin", garmin_activity_id=19876543210)
        parent = parsed.parent
        assert parent is not None
        assert parent.activity_id == 19876543210
        children = [a for a in parsed.activities if a is not parent]
        assert all(is_synthetic_id(c.activity_id) for c in children)

    def test_all_ids_are_unique(self, triathlon_fit: Path) -> None:
        parsed = parse_fit(triathlon_fit)
        ids = [a.activity_id for a in parsed.activities]
        assert len(set(ids)) == len(ids)

    def test_parent_sums_distance_across_legs(self, triathlon_fit: Path) -> None:
        parent = parse_fit(triathlon_fit).parent
        assert parent is not None
        assert parent.total_distance_m == pytest.approx(51500.0)  # 1500 + 40000 + 10000

    def test_parent_heart_rate_is_weighted_by_moving_time(self, triathlon_fit: Path) -> None:
        # A 90-second transition must not count as much as a 70-minute bike leg.
        parent = parse_fit(triathlon_fit).parent
        assert parent is not None
        assert parent.avg_heart_rate is not None
        assert 150 <= parent.avg_heart_rate <= 158

    def test_records_are_assigned_to_the_right_leg(self, triathlon_fit: Path) -> None:
        parsed = parse_fit(triathlon_fit)
        for leg in (a for a in parsed.activities if a.parent_activity_id is not None):
            assert len(leg.records) == 20
            end = leg.end_time_utc
            assert end is not None
            assert all(leg.start_time_utc <= r.ts <= end for r in leg.records)

    def test_parent_owns_no_samples(self, triathlon_fit: Path) -> None:
        # Records belong to the legs; duplicating them would double every total.
        parent = parse_fit(triathlon_fit).parent
        assert parent is not None
        assert parent.records == []
        assert parent.laps == []

    def test_per_discipline_volume_stays_queryable(self, triathlon_fit: Path) -> None:
        # The whole point of the parent/child split: the 10 km run inside a
        # triathlon must still count towards running volume.
        run_leg = _leg(parse_fit(triathlon_fit), "running")
        assert run_leg.total_distance_m == pytest.approx(10000.0)


class TestDegradedInputs:
    """Real devices drop sensors; downloads get truncated."""

    def test_missing_gps_leaves_positions_null(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "indoor.fit", fit_builder.build_run(with_gps=False))
        activity = parse_fit(path).activities[0]
        assert activity.start_lat is None
        assert all(r.lat is None and r.lon is None for r in activity.records)
        # Everything else must still be there.
        assert activity.total_distance_m == pytest.approx(6000.0)

    def test_missing_heart_rate_leaves_it_null(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "nohr.fit", fit_builder.build_run(with_hr=False))
        activity = parse_fit(path).activities[0]
        assert activity.avg_heart_rate is None
        assert all(r.heart_rate is None for r in activity.records)

    def test_truncated_file_raises_a_typed_error(self, corrupt_fit: Path) -> None:
        with pytest.raises(FitParseError):
            parse_fit(corrupt_fit)

    def test_file_without_session_is_rejected_with_an_explanation(
        self, non_activity_fit: Path
    ) -> None:
        with pytest.raises(FitParseError, match="no session message"):
            parse_fit(non_activity_fit)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FitParseError, match="does not exist"):
            parse_fit(tmp_path / "nope.fit")


class TestIdentity:
    """Ids and hashes must be stable across runs and machines."""

    def test_hash_is_content_addressed(self, tmp_path: Path) -> None:
        data = fit_builder.build_run()
        a = _write(tmp_path, "a.fit", data)
        b = _write(tmp_path, "b.fit", data)  # same bytes, different name
        assert hash_fit_file(a) == hash_fit_file(b)

    def test_synthetic_ids_are_deterministic(self, run_fit: Path) -> None:
        first = parse_fit(run_fit).activities[0].activity_id
        second = parse_fit(run_fit).activities[0].activity_id
        assert first == second
        assert is_synthetic_id(first)

    def test_synthetic_ids_cannot_collide_with_garmin_ids(self, run_fit: Path) -> None:
        # Real Garmin ids are ~2e10 in 2026; synthetic ones sit above 2^62.
        activity_id = parse_fit(run_fit).activities[0].activity_id
        assert activity_id > 2**62
        assert activity_id < 2**63  # still fits a signed BIGINT

    def test_garmin_id_is_used_when_supplied(self, run_fit: Path) -> None:
        activity = parse_fit(run_fit, source="garmin", garmin_activity_id=12345).activities[0]
        assert activity.activity_id == 12345
        assert activity.garmin_activity_id == 12345
        assert activity.source == "garmin"


# ─── helpers ──────────────────────────────────────────────────────────


def _write(directory: Path, name: str, data: bytes) -> Path:
    path = directory / name
    path.write_bytes(data)
    return path


def _leg(parsed, sport: str):  # type: ignore[no-untyped-def]
    return next(a for a in parsed.activities if a.sport == sport and a.parent_activity_id)


class TestDeviceAgnosticHeuristics:
    """Behaviours discovered by running the parser over a corpus of real files.

    Each test here encodes a quirk observed on actual hardware — a Garmin fr70,
    a Wahoo ELEMNT, a Coros Pace 2, the Strava Android app — reproduced
    synthetically so the suite stays hermetic. The corpus itself is not
    committed; see tests/test_corpus.py for the opt-in run against it.
    """

    def test_a_file_concatenated_with_itself_yields_one_activity(self, tmp_path: Path) -> None:
        # Observed in activity-activity-filecrc.fit: two headers, two identical
        # sessions. Counting it twice would inflate every weekly total.
        path = _write(tmp_path, "twice.fit", fit_builder.build_concatenated(2))
        parsed = parse_fit(path)
        assert len(parsed.activities) == 1
        assert not parsed.is_multisport

    def test_two_unrelated_recordings_are_not_a_triathlon(self, tmp_path: Path) -> None:
        # Two sessions, but two `activity` messages and three days apart.
        path = _write(tmp_path, "chained.fit", fit_builder.build_chained_distinct())
        parsed = parse_fit(path)
        assert not parsed.is_multisport
        assert parsed.parent is None
        assert len(parsed.activities) == 2
        assert all(a.parent_activity_id is None for a in parsed.activities)

    def test_missing_activity_message_falls_back_to_longitude(self, tmp_path: Path) -> None:
        # Several Edge and fr110 files carry no activity message at all.
        # Pretending the athlete lives in UTC files a Sunday run under Monday.
        path = _write(
            tmp_path,
            "noactivity.fit",
            fit_builder.build_run(with_activity_msg=False, longitude=4.85),
        )
        activity = parse_fit(path).activities[0]
        assert activity.tz_offset_seconds == 0  # 4.85°E rounds to UTC+0
        assert activity.extra["_tz_offset_source"] == "longitude_estimate"

    def test_longitude_fallback_lands_on_the_right_side_of_the_globe(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "tokyo.fit",
            fit_builder.build_run(with_activity_msg=False, longitude=139.7),
        )
        activity = parse_fit(path).activities[0]
        assert activity.tz_offset_seconds == 9 * 3600

    def test_offset_is_marked_unavailable_when_there_is_no_signal(self, tmp_path: Path) -> None:
        # An indoor trainer session: no activity message, no GPS.
        path = _write(
            tmp_path,
            "indoor.fit",
            fit_builder.build_run(with_activity_msg=False, with_gps=False),
        )
        activity = parse_fit(path).activities[0]
        assert activity.tz_offset_seconds is None
        assert activity.extra["_tz_offset_source"] == "unavailable"

    def test_exact_offset_is_not_flagged_as_reconstructed(self, run_fit: Path) -> None:
        # Provenance markers must appear only when a value was inferred.
        activity = parse_fit(run_fit).activities[0]
        assert "_tz_offset_source" not in activity.extra
        assert "_start_time_source" not in activity.extra

    def test_unresolvable_product_code_keeps_the_manufacturer(self, tmp_path: Path) -> None:
        # Wahoo writes product 28; the FIT profile cannot name it, and a bare
        # "28" in the database is useless.
        path = _write(
            tmp_path,
            "wahoo.fit",
            fit_builder.build_run(manufacturer=32, product=28),  # 32 = wahoo_fitness
        )
        activity = parse_fit(path).activities[0]
        assert activity.device_product is not None
        assert "28" in activity.device_product
        assert "wahoo" in activity.device_product.lower()

    def test_truncated_file_keeps_the_sessions_that_survived(self, tmp_path: Path) -> None:
        # The Strava Android export breaks mid-file but has already written a
        # complete session; discarding the whole ride would lose real data.
        good = fit_builder.build_run(num_records=40)
        # Chop the trailing CRC and part of the activity message.
        path = _write(tmp_path, "cut.fit", good[:-8])
        parsed = parse_fit(path)
        assert len(parsed.activities) == 1
        assert parsed.activities[0].extra.get("_truncated")

    def test_partial_recovery_is_refused_when_no_session_survived(self, corrupt_fit: Path) -> None:
        # nick.fit loses its session to truncation: nothing usable remains.
        with pytest.raises(FitParseError):
            parse_fit(corrupt_fit)

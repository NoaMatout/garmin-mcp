"""Tests for find_local_activities, get_activity_route and the GPX resources.

Everything runs on synthetic activities inserted into a temporary database
(see geo_fixtures). Two guarantees run through all of it:

* **Honesty about the data.** Nothing is invented, interpolated or merged: a
  missing altitude stays missing, a recording gap stays a gap, the second lap
  of a loop stays a second lap.
* **Locality.** These tools read the database and nothing else, so they keep
  working when Garmin is unreachable. One test makes that a hard failure by
  forbidding network sockets.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from garmin_mcp.config import Settings
from garmin_mcp.db.connection import writing
from garmin_mcp.db.migrations import init_database
from garmin_mcp.errors import ActivityNotFoundError, RouteUnavailableError
from garmin_mcp.server import route_tools
from garmin_mcp.server.app import create_server
from tests.geo_fixtures import KM_LAT, REIMS, Fix, line, loop, seed_activity

NS = {"g": "http://www.topografix.com/GPX/1/1"}


def _north(km: float) -> tuple[float, float]:
    """A start point `km` kilometres north of the Reims anchor."""
    return (REIMS[0] + km * KM_LAT, REIMS[1])


@pytest.fixture
def db(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    init_database(settings)
    monkeypatch.setattr(route_tools, "_settings", lambda: settings)
    return settings


def _seed(settings: Settings, *activities: dict[str, Any]) -> None:
    with writing(settings) as conn:
        for spec in activities:
            seed_activity(conn, **spec)


def _find(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"latitude": REIMS[0], "longitude": REIMS[1], "radius_km": 10}
    return route_tools.find_local_activities(**(args | overrides))


def _ids(result: dict[str, Any]) -> list[int]:
    return [a["activity_id"] for a in result["activities"]]


class TestRadius:
    def test_only_activities_starting_inside_the_circle_are_found(self, db: Settings) -> None:
        _seed(
            db,
            {"activity_id": 1, "fixes": line(20), "start": _north(2)},
            {"activity_id": 2, "fixes": line(20), "start": _north(9.5)},
            {"activity_id": 3, "fixes": line(20), "start": _north(10.5)},
            {"activity_id": 4, "fixes": line(20), "start": (48.8566, 2.3522)},  # Paris
        )
        assert sorted(_ids(_find())) == [1, 2]

    def test_a_corner_of_the_bounding_box_is_outside_the_circle(self, db: Settings) -> None:
        """9 km north and 9 km east sits inside the square but 12.7 km away."""
        import math

        east = 9 * KM_LAT / math.cos(math.radians(REIMS[0]))
        corner = (REIMS[0] + 9 * KM_LAT, REIMS[1] + east)
        _seed(db, {"activity_id": 1, "fixes": None, "start": corner})
        assert _find()["total_matches"] == 0
        assert _find(radius_km=13)["total_matches"] == 1

    def test_the_radius_applies_to_the_start_not_to_the_track(self, db: Settings) -> None:
        """A run that starts 30 km away and crosses the centre is not a local run."""
        crossing = line(40, lat=REIMS[0] - 20 * KM_LAT, d_lat=KM_LAT)
        _seed(db, {"activity_id": 1, "fixes": crossing, "start": _north(30)})
        assert _find()["total_matches"] == 0

    def test_results_say_how_far_the_start_is_and_what_that_means(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(20), "start": _north(4)})
        result = _find()
        assert result["activities"][0]["start_point_distance_km"] == pytest.approx(4.0, abs=0.05)
        assert "START" in result["query"]["radius_means"]
        assert "travel" in result["query"]["radius_means"]

    def test_an_activity_without_a_start_position_is_never_found(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": None, "start": None, "sub_sport": "treadmill"})
        assert _find(radius_km=300)["total_matches"] == 0

    def test_a_search_across_the_antimeridian_still_works(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": None, "start": (-17.0, -179.95)})
        result = route_tools.find_local_activities(-17.0, 179.95, radius_km=20)
        assert _ids(result) == [1]

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"latitude": 91}, "latitude"),
            ({"longitude": -181}, "longitude"),
            ({"radius_km": 0}, "radius_km"),
            ({"radius_km": 301}, "radius_km"),
            ({"min_distance_km": 20, "max_distance_km": 10}, "min_distance_km"),
            ({"min_ascent_m": -1}, "negative"),
            ({"sort": "random"}, "unknown sort"),
            ({"duration_basis": "wall"}, "duration_basis"),
        ],
    )
    def test_bad_input_is_refused_with_a_reason(
        self, db: Settings, overrides: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            _find(**overrides)


class TestFilters:
    def test_sport_defaults_to_running_and_can_be_changed(self, db: Settings) -> None:
        _seed(
            db,
            {"activity_id": 1, "fixes": None},
            {"activity_id": 2, "fixes": None, "sport": "cycling", "sub_sport": "road"},
        )
        assert _ids(_find()) == [1]
        assert _ids(_find(activity_type="cycling")) == [2]
        assert sorted(_ids(_find(activity_type=None))) == [1, 2]

    def test_sub_sport_narrows_to_trail(self, db: Settings) -> None:
        _seed(
            db,
            {"activity_id": 1, "fixes": None, "sub_sport": "trail"},
            {"activity_id": 2, "fixes": None, "sub_sport": "street"},
        )
        assert _ids(_find(sub_sport="trail")) == [1]

    def test_distance_ascent_and_duration_ranges(self, db: Settings) -> None:
        _seed(
            db,
            {"activity_id": 1, "fixes": None, "distance_m": 12_000, "ascent_m": 200},
            {"activity_id": 2, "fixes": None, "distance_m": 30_000, "ascent_m": 900},
            {"activity_id": 3, "fixes": None, "distance_m": 56_000, "ascent_m": 1400},
        )
        assert sorted(_ids(_find(min_distance_km=25))) == [2, 3]
        assert _ids(_find(min_distance_km=25, max_distance_km=40)) == [2]
        assert sorted(_ids(_find(min_ascent_m=800))) == [2, 3]
        assert _ids(_find(max_ascent_m=300)) == [1]

    def test_moving_and_elapsed_time_are_distinct_and_the_choice_is_honoured(
        self, db: Settings
    ) -> None:
        # 3 h moving, 4 h elapsed: it passes a 3.5 h ceiling on one basis only.
        _seed(db, {"activity_id": 1, "fixes": None, "timer_s": 3 * 3600, "elapsed_s": 4 * 3600})
        assert _ids(_find(max_duration_min=210, duration_basis="moving")) == [1]
        assert _ids(_find(max_duration_min=210, duration_basis="elapsed")) == []

        measures = _find()["activities"][0]["measures"]
        assert measures["moving_time_s"] == 3 * 3600
        assert measures["elapsed_time_s"] == 4 * 3600
        assert measures["moving_time"] == "3:00:00"
        assert measures["elapsed_time"] == "4:00:00"

    def test_a_missing_measure_cannot_satisfy_a_range(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": None, "ascent_m": None})
        assert _ids(_find()) == [1]
        assert _ids(_find(min_ascent_m=0)) == []


class TestPagination:
    @pytest.fixture
    def many(self, db: Settings) -> Settings:
        _seed(
            db,
            *[
                {"activity_id": i, "fixes": None, "start": _north(i * 0.5), "day": i}
                for i in range(1, 8)
            ],
        )
        return db

    def test_pages_are_consistent_and_complete(self, many: Settings) -> None:
        first = _find(limit=3)
        second = _find(limit=3, offset=first["next_offset"])
        third = _find(limit=3, offset=second["next_offset"])
        assert first["total_matches"] == 7 and first["has_more"] and first["returned"] == 3
        assert second["has_more"] and not third["has_more"] and "next_offset" not in third
        assert _ids(first) + _ids(second) + _ids(third) == [1, 2, 3, 4, 5, 6, 7]

    def test_a_page_past_the_end_is_empty_but_still_reports_the_total(self, many: Settings) -> None:
        result = _find(limit=3, offset=50)
        assert result["activities"] == [] and result["total_matches"] == 7
        assert not result["has_more"]

    def test_limit_is_capped(self, many: Settings) -> None:
        assert _find(limit=10_000)["returned"] == 7
        assert route_tools.MAX_FIND_LIMIT == 100

    def test_sort_by_recent_reverses_the_order(self, many: Settings) -> None:
        assert _ids(_find(sort="recent")) == [7, 6, 5, 4, 3, 2, 1]
        assert _ids(_find(sort="nearest")) == [1, 2, 3, 4, 5, 6, 7]

    def test_an_empty_search_explains_what_to_try(self, db: Settings) -> None:
        result = _find()
        assert result["total_matches"] == 0 and "Widen" in result["note"]


class TestProvenanceAndQuality:
    def test_measures_are_the_summary_values_not_a_recomputation(self, db: Settings) -> None:
        """The track is ~190 m long; the summary says 30 km. The summary wins."""
        _seed(
            db,
            {
                "activity_id": 1,
                "fixes": line(20),
                "distance_m": 30_000.0,
                "ascent_m": 657.0,
                "timer_s": 12_001.0,
            },
        )
        entry = _find()["activities"][0]
        assert entry["measures"]["distance_km"] == 30.0
        assert entry["measures"]["ascent_m"] == 657
        assert "FIT session summary" in _find()["measures_source"]
        assert "not recomputed" in _find()["measures_source"]

    def test_a_clean_track_has_gps_altitude_and_is_not_partial(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(100)})
        quality = _find()["activities"][0]["quality"]
        assert quality["has_gps"] and quality["has_altitude"]
        assert quality["gps_points"] == 100 and quality["gps_coverage"] == 1.0
        assert quality["partial_track"] is False and quality["partial_reasons"] == []

    def test_no_altitude_is_reported_as_such(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(100, alt=None)})
        quality = _find()["activities"][0]["quality"]
        assert quality["has_gps"] and not quality["has_altitude"]

    def test_a_recording_gap_makes_the_track_partial_and_says_why(self, db: Settings) -> None:
        fixes = line(50) + line(50, t0=600, lat=REIMS[0] + 0.05)
        _seed(db, {"activity_id": 1, "fixes": fixes})
        quality = _find()["activities"][0]["quality"]
        assert quality["partial_track"] is True
        assert quality["interruptions"] == 1
        assert quality["longest_gap_s"] == pytest.approx(551, abs=1)
        assert any("gap" in reason for reason in quality["partial_reasons"])

    def test_samples_without_a_fix_lower_the_coverage(self, db: Settings) -> None:
        fixes = line(50) + [Fix(50 + i, None, None, None) for i in range(50)]
        _seed(db, {"activity_id": 1, "fixes": fixes})
        quality = _find()["activities"][0]["quality"]
        assert quality["gps_coverage"] == 0.5 and quality["partial_track"] is True
        assert any("50%" in reason for reason in quality["partial_reasons"])

    def test_a_late_first_fix_and_a_dead_tail_are_flagged(self, db: Settings) -> None:
        fixes = (
            [Fix(i, None, None, None) for i in range(120)]
            + line(200, t0=120)
            + [Fix(320 + i, None, None, None) for i in range(120)]
        )
        _seed(db, {"activity_id": 1, "fixes": fixes})
        reasons = " ".join(_find()["activities"][0]["quality"]["partial_reasons"])
        assert "first fix" in reasons and "last" in reasons

    def test_an_activity_with_only_a_summary_says_so(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": None})
        quality = _find()["activities"][0]["quality"]
        assert quality["has_gps"] is False and quality["samples"] == 0
        assert quality["partial_track"] is None  # unknowable, not "complete"

    def test_null_island_fixes_do_not_count_as_gps(self, db: Settings) -> None:
        fixes = [Fix(i, 0.0, 0.0, None) for i in range(30)]
        _seed(db, {"activity_id": 1, "fixes": fixes})
        quality = _find()["activities"][0]["quality"]
        assert quality["has_gps"] is False


class TestActivityRoute:
    def test_the_track_is_read_from_the_stored_samples_not_the_averaged_stream(
        self, db: Settings
    ) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(1500)})
        route = route_tools.get_activity_route(1, max_points=2000)
        assert route["track"]["simplification"]["points_available"] == 1500
        assert route["track"]["simplification"]["points_returned"] == 1500
        assert route["track"]["simplification"]["simplified"] is False
        assert len(route["track"]["segments"][0]["lat"]) == 1500

    def test_the_response_is_bounded_and_the_simplification_is_disclosed(
        self, db: Settings
    ) -> None:
        _seed(db, {"activity_id": 1, "fixes": loop(2, points_per_lap=400)})
        route = route_tools.get_activity_route(1, max_points=100)
        simplification = route["track"]["simplification"]
        assert simplification["points_available"] == 801
        assert simplification["points_returned"] == 100
        assert simplification["simplified"] is True
        assert simplification["max_deviation_m"] > 0
        assert sum(len(s["lat"]) for s in route["track"]["segments"]) == 100

    def test_max_points_is_capped(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(10)})
        assert route_tools.MAX_ROUTE_POINTS == 2000
        assert (
            route_tools.get_activity_route(1, max_points=10**9)["track"]["simplification"][
                "points_returned"
            ]
            == 10
        )

    def test_an_interruption_is_two_segments_and_a_listed_gap(self, db: Settings) -> None:
        fixes = line(30) + line(30, t0=1000, lat=REIMS[0] + 0.02)
        _seed(db, {"activity_id": 1, "fixes": fixes})
        route = route_tools.get_activity_route(1)
        assert len(route["track"]["segments"]) == 2
        assert route["interruptions"][0]["gap_s"] == pytest.approx(971, abs=1)
        assert any("not joined" in item for item in route["missing_data"])
        assert route["gpx"]["segments"] == 2

    def test_no_altitude_is_never_invented(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(40, alt=None)})
        route = route_tools.get_activity_route(1)
        assert "altitude_m" not in route["track"]["segments"][0]
        assert any("altitude absent on 40 of 40" in item for item in route["missing_data"])

    def test_partial_altitude_leaves_nulls_where_it_is_missing(self, db: Settings) -> None:
        fixes = line(10)
        fixes[3] = Fix(fixes[3].elapsed_s, fixes[3].lat, fixes[3].lon, None, 30.0)
        _seed(db, {"activity_id": 1, "fixes": fixes})
        altitudes = route_tools.get_activity_route(1)["track"]["segments"][0]["altitude_m"]
        assert altitudes[3] is None and altitudes[2] is not None

    def test_the_cumulative_distance_is_the_devices_not_a_recomputation(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(5, step_m=1000.0)})
        distances = route_tools.get_activity_route(1)["track"]["segments"][0]["distance_km"]
        assert distances == [0.0, 1.0, 2.0, 3.0, 4.0]  # steps are 1 km regardless of lat/lon

    def test_summary_figures_keep_their_source(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(10), "distance_m": 21_097.0, "ascent_m": 410.0})
        summary = route_tools.get_activity_route(1)["summary"]
        assert summary["distance_km"] == 21.1 and summary["ascent_m"] == 410
        assert summary["source"] == "FIT session summary"

    def test_a_repeated_loop_keeps_both_passes_in_the_track_and_the_gpx(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": loop(2, points_per_lap=60)})
        route = route_tools.get_activity_route(1, max_points=2000)
        elapsed = route["track"]["segments"][0]["elapsed_s"]
        assert len(elapsed) == 121 and elapsed == sorted(elapsed)

        text = route_tools.activity_route_gpx("1")
        assert len(ET.fromstring(text).findall(".//g:trkpt", NS)) == 121  # nothing deduplicated

    def test_an_out_and_back_is_not_folded_onto_itself(self, db: Settings) -> None:
        out = line(50)
        back = [
            Fix(50 + i, f.lat, f.lon, f.alt, 500 + i * 10.0) for i, f in enumerate(reversed(out))
        ]
        _seed(db, {"activity_id": 1, "fixes": out + back})
        route = route_tools.get_activity_route(1, max_points=2000)
        assert route["track"]["simplification"]["points_returned"] == 100

    def test_an_activity_without_gps_returns_no_track_and_says_so(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": [Fix(i, None, None, None) for i in range(20)]})
        route = route_tools.get_activity_route(1)
        assert route["track"] is None and route["gpx"] is None
        assert any("no valid GPS fix" in note for note in route["notes"])

    def test_an_unknown_activity_raises_with_a_way_forward(self, db: Settings) -> None:
        with pytest.raises(ActivityNotFoundError):
            route_tools.get_activity_route(999)

    def test_the_response_never_claims_the_route_is_validated(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(10)})
        notes = " ".join(route_tools.get_activity_route(1)["notes"])
        assert "not a validated or public route" in notes
        assert "water points" in notes and "access rights" in notes


class TestGpxResource:
    def test_the_gpx_is_full_resolution_and_matches_the_advertised_hash(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(900)})
        route = route_tools.get_activity_route(1, max_points=50)
        text = route_tools.activity_route_gpx("1")

        assert route["gpx"]["points"] == 900
        assert len(ET.fromstring(text).findall(".//g:trkpt", NS)) == 900  # not the 50 shown
        assert route["gpx"]["bytes"] == len(text.encode())
        assert route["gpx"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()

    def test_gaps_become_separate_trksegs(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(10) + line(10, t0=500, lat=REIMS[0] + 0.05)})
        root = ET.fromstring(route_tools.activity_route_gpx("1"))
        assert [len(s) for s in root.findall(".//g:trkseg", NS)] == [10, 10]

    def test_only_valid_fixes_are_exported_and_missing_altitude_is_omitted(
        self, db: Settings
    ) -> None:
        fixes = [Fix(0, REIMS[0], REIMS[1], 100.0), Fix(1, None, None, 100.0)]
        fixes += [Fix(2, REIMS[0] + 1e-4, REIMS[1], None)]
        _seed(db, {"activity_id": 1, "fixes": fixes})
        points = ET.fromstring(route_tools.activity_route_gpx("1")).findall(".//g:trkpt", NS)
        assert len(points) == 2
        assert points[0].find("g:ele", NS) is not None and points[1].find("g:ele", NS) is None

    def test_unusable_requests_fail_clearly(self, db: Settings) -> None:
        _seed(db, {"activity_id": 1, "fixes": None})
        with pytest.raises(ValueError, match="no valid GPS fix"):
            route_tools.activity_route_gpx("1")
        with pytest.raises(ValueError, match="must be an integer"):
            route_tools.activity_route_gpx("../etc/passwd")
        with pytest.raises(ActivityNotFoundError):
            route_tools.activity_route_gpx("424242")

    def test_the_resource_is_served_over_mcp_without_any_local_path(self, db: Settings) -> None:
        """What a remote client does: ask the server for the URI and get the bytes."""
        _seed(db, {"activity_id": 1, "fixes": line(30)})
        server = create_server()

        templates = asyncio.run(server.list_resource_templates())
        assert any(t.uri_template == route_tools.ACTIVITY_GPX_URI for t in templates)

        contents = list(asyncio.run(server.read_resource("garmin://activity/1/route.gpx")))
        assert len(contents) == 1
        assert len(ET.fromstring(contents[0].content).findall(".//g:trkpt", NS)) == 30

    def test_a_saved_course_exports_its_stored_geometry(self, db: Settings) -> None:
        from garmin_mcp.domain.models import Course, CourseTrackPoint
        from garmin_mcp.ingest.course_writer import write_course_detail, write_course_summary

        course = Course(course_id=7, name="Boucle d'essai", sport="trail_running")
        course.track = [
            CourseTrackPoint(i, REIMS[0] + i * 1e-4, REIMS[1], 150.0 + i, i * 11.0)
            for i in range(5)
        ]
        with writing(db) as conn:
            write_course_summary(conn, course)
            write_course_detail(conn, course)

        root = ET.fromstring(route_tools.course_route_gpx("7"))
        assert len(root.findall(".//g:trkpt", NS)) == 5
        assert root.findtext("g:trk/g:name", namespaces=NS) == "Boucle d'essai"

        with pytest.raises(RouteUnavailableError):
            route_tools.course_route_gpx("8")


class TestLocality:
    def test_these_tools_work_with_the_network_forbidden(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired Garmin session must not stop a local read: here, no socket at all."""
        _seed(db, {"activity_id": 1, "fixes": line(30)})

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("a local read tried to open a network connection")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        assert _ids(_find()) == [1]
        assert route_tools.get_activity_route(1)["track"] is not None
        assert route_tools.activity_route_gpx("1")

    def test_logs_carry_counts_not_coordinates(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db, {"activity_id": 1, "fixes": line(30)})
        logged: list[dict[str, Any]] = []

        class Spy:
            def info(self, event: str, **fields: Any) -> None:
                logged.append({"event": event, **fields})

        monkeypatch.setattr(route_tools, "log", Spy())
        _find()
        route_tools.get_activity_route(1)

        assert logged
        text = repr(logged)
        assert str(REIMS[0]) not in text and str(REIMS[1]) not in text
        assert "49.25" not in text and "4.03" not in text


class TestEndToEndFromAFitFile:
    def test_a_run_ingested_from_fit_is_found_and_its_track_exported(self, db: Settings) -> None:
        """The same tools over data that went through the real parser and writer."""
        from garmin_mcp.ingest.pipeline import import_inbox
        from tests import fit_builder

        (db.inbox_dir / "run.fit").write_bytes(
            fit_builder.build_run(num_records=60, distance_m=6000.0, duration_s=1800)
        )
        import_inbox(db)

        result = route_tools.find_local_activities(45.75, 4.85, radius_km=5)
        assert result["total_matches"] == 1
        entry = result["activities"][0]
        assert entry["quality"]["has_gps"] and entry["quality"]["gps_points"] == 60

        route = route_tools.get_activity_route(entry["activity_id"], max_points=2000)
        assert route["gpx"]["points"] == 60


class TestExportGpxCommand:
    def _run(self, db: Settings, *args: str) -> Any:
        from typer.testing import CliRunner

        from garmin_mcp.cli import app

        return CliRunner().invoke(app, ["export-gpx", *args])

    def test_writes_the_full_track_to_the_requested_file(
        self, db: Settings, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("garmin_mcp.cli.get_settings", lambda: db)
        _seed(db, {"activity_id": 1, "fixes": line(10) + line(10, t0=900, lat=REIMS[0] + 0.1)})
        target = tmp_path / "out.gpx"

        result = self._run(db, "1", "-o", str(target))

        assert result.exit_code == 0, result.output
        assert "1 gap(s) not bridged" in result.output
        assert len(ET.fromstring(target.read_text()).findall(".//g:trkpt", NS)) == 20

    def test_refuses_to_overwrite_without_force(
        self, db: Settings, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("garmin_mcp.cli.get_settings", lambda: db)
        _seed(db, {"activity_id": 1, "fixes": line(5)})
        target = tmp_path / "out.gpx"
        target.write_text("keep me")

        assert self._run(db, "1", "-o", str(target)).exit_code == 1
        assert target.read_text() == "keep me"
        assert self._run(db, "1", "-o", str(target), "--force").exit_code == 0

    def test_an_activity_without_gps_fails_cleanly(
        self, db: Settings, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("garmin_mcp.cli.get_settings", lambda: db)
        _seed(db, {"activity_id": 1, "fixes": None})
        result = self._run(db, "1", "-o", str(tmp_path / "x.gpx"))
        assert result.exit_code == 1 and "no valid GPS fix" in result.output
        assert not (tmp_path / "x.gpx").exists()

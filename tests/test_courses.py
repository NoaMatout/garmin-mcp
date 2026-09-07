"""Tests for saved courses: parsing, writing, syncing, and the MCP tools.

No network, same rule as test_sleep.py and test_garmin_sync.py.
`course_reader.list_courses` / `.get_course` are monkeypatched to stand in
for Garmin.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from garmin_mcp.config import Settings
from garmin_mcp.db.migrations import init_database
from garmin_mcp.errors import GarminError
from garmin_mcp.ingest.course_parser import attach_detail, parse_course_summary
from garmin_mcp.ingest.course_writer import write_course_detail, write_course_summary
from garmin_mcp.ingest.pipeline import sync_courses_from_garmin
from garmin_mcp.ingest.worker import IngestWorker


def _raw_summary(
    course_id: int,
    *,
    name: str = "Trail du Charvin",
    sport: str = "trail_running",
    distance_m: float = 16210.83,
    gain_m: float = 1119.47,
    favorite: bool = False,
) -> dict[str, Any]:
    return {
        "courseId": course_id,
        "courseName": name,
        "activityType": {"typeKey": sport},
        "distanceInMeters": distance_m,
        "elevationGainInMeters": gain_m,
        "elevationLossInMeters": gain_m - 2.0,
        "startLatitude": 45.24,
        "startLongitude": 6.27,
        "createdDate": 1_757_130_600_000,
        "updatedDate": 1_757_130_600_000,
        "favorite": favorite,
    }


def _raw_detail(course_id: int, *, num_points: int = 5, waypoint: bool = True) -> dict[str, Any]:
    geo_points = [
        {
            "latitude": 45.24 + i * 0.001,
            "longitude": 6.27,
            "elevation": 1500.0 + i * 10,
            "distance": i * 100.0,
        }
        for i in range(num_points)
    ]
    course_points = (
        [
            {
                "name": "Ravitaillement",
                "coursePointType": "GENERIC",
                "distance": 250.0,
                "lat": 45.241,
                "lon": 6.27,
                "elevation": 0.0,
                "derivedElevation": 1510.0,
            }
        ]
        if waypoint
        else []
    )
    return {
        "courseId": course_id,
        "distanceMeter": num_points * 100.0,
        "geoPoints": geo_points,
        "coursePoints": course_points,
        "boundingBox": {"lowerLeft": {}, "upperRight": {}},
    }


@pytest.fixture
def db(settings: Settings) -> Settings:
    init_database(settings)
    return settings


def _courses(settings: Settings) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        return conn.execute(
            "SELECT course_id, name, sport, favorite FROM courses ORDER BY course_id"
        ).fetchall()


def _points(settings: Settings, course_id: int) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        return conn.execute(
            "SELECT point_index, elevation_m FROM course_points "
            "WHERE course_id = ? ORDER BY point_index",
            [course_id],
        ).fetchall()


class TestCourseParser:
    def test_parses_the_listing_summary(self) -> None:
        course = parse_course_summary(_raw_summary(1, name="Verzy simple", favorite=True))
        assert course.course_id == 1
        assert course.name == "Verzy simple"
        assert course.sport == "trail_running"
        assert course.distance_m == 16210.83
        assert course.elevation_gain_m == 1119.47
        assert course.favorite is True
        assert course.created_at is not None

    def test_attach_detail_fills_track_and_waypoints(self) -> None:
        course = parse_course_summary(_raw_summary(1))
        detail = _raw_detail(1, num_points=3)
        attach_detail(course, detail)

        assert len(course.track) == 3
        assert course.track[0].elevation_m == 1500.0
        assert course.track[2].point_index == 2
        assert len(course.waypoints) == 1
        assert course.waypoints[0].name == "Ravitaillement"
        assert course.waypoints[0].elevation_m == 1510.0  # derivedElevation wins over elevation=0

    def test_geo_points_are_not_duplicated_into_extra(self) -> None:
        """They are normalised into course_points — keeping them in `extra`
        too would double the storage for no reader that needs it."""
        course = parse_course_summary(_raw_summary(1))
        attach_detail(course, _raw_detail(1))
        assert "geoPoints" not in course.extra
        assert "coursePoints" not in course.extra
        assert "boundingBox" in course.extra  # everything else still kept


class TestCourseWriter:
    def test_summary_upsert_replaces_in_place(self, db: Settings) -> None:
        with duckdb.connect(str(db.db_path)) as conn:
            write_course_summary(conn, parse_course_summary(_raw_summary(1, favorite=False)))
            write_course_summary(conn, parse_course_summary(_raw_summary(1, favorite=True)))

        rows = _courses(db)
        assert len(rows) == 1
        assert rows[0][3] is True

    def test_detail_rewrite_drops_stale_points(self, db: Settings) -> None:
        course = parse_course_summary(_raw_summary(1))
        with duckdb.connect(str(db.db_path)) as conn:
            write_course_summary(conn, course)
            attach_detail(course, _raw_detail(1, num_points=5))
            write_course_detail(conn, course)

            attach_detail(course, _raw_detail(1, num_points=2))
            write_course_detail(conn, course)

        assert len(_points(db, 1)) == 2


class TestSyncCoursesFromGarmin:
    def test_fetches_detail_only_for_new_courses(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetched: list[int] = []

        monkeypatch.setattr(
            "garmin_mcp.garmin.course_reader.list_courses",
            lambda _settings=None: [_raw_summary(1), _raw_summary(2)],
        )

        def fake_get(course_id: int, _settings: Settings | None = None) -> dict[str, Any]:
            fetched.append(course_id)
            return _raw_detail(course_id)

        monkeypatch.setattr("garmin_mcp.garmin.course_reader.get_course", fake_get)

        first = sync_courses_from_garmin(db)
        assert first.summaries_refreshed == 2
        assert first.details_fetched == 2
        assert sorted(fetched) == [1, 2]

        second = sync_courses_from_garmin(db)
        assert second.summaries_refreshed == 2
        assert second.details_fetched == 0
        assert sorted(fetched) == [1, 2]  # nothing re-fetched

    def test_a_renamed_course_is_refreshed_without_a_new_detail_fetch(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "garmin_mcp.garmin.course_reader.list_courses",
            lambda _settings=None: [_raw_summary(1, name="Old name")],
        )
        monkeypatch.setattr(
            "garmin_mcp.garmin.course_reader.get_course",
            lambda course_id, _settings=None: _raw_detail(course_id),
        )
        sync_courses_from_garmin(db)

        monkeypatch.setattr(
            "garmin_mcp.garmin.course_reader.list_courses",
            lambda _settings=None: [_raw_summary(1, name="New name")],
        )
        report = sync_courses_from_garmin(db)

        assert report.details_fetched == 0
        assert _courses(db)[0][1] == "New name"

    def test_a_detail_failure_for_one_course_does_not_abort_the_rest(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "garmin_mcp.garmin.course_reader.list_courses",
            lambda _settings=None: [_raw_summary(1), _raw_summary(2)],
        )

        def flaky(course_id: int, _settings: Settings | None = None) -> dict[str, Any]:
            if course_id == 1:
                raise GarminError("transient failure")
            return _raw_detail(course_id)

        monkeypatch.setattr("garmin_mcp.garmin.course_reader.get_course", flaky)

        report = sync_courses_from_garmin(db)

        assert report.details_fetched == 1
        assert report.failed == 1
        assert len(_points(db, 2)) > 0
        assert _points(db, 1) == []

    def test_no_courses_is_not_an_error(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "garmin_mcp.garmin.course_reader.list_courses", lambda _settings=None: []
        )
        report = sync_courses_from_garmin(db)
        assert report.summaries_refreshed == 0
        assert _courses(db) == []


class TestWorkerIntegration:
    def test_a_successful_cycle_includes_a_course_report(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.ingest.pipeline import CourseSyncReport, SleepSyncReport

        worker = IngestWorker(db)
        monkeypatch.setattr(
            IngestWorker, "_sync_garmin", lambda _self, **_k: _EmptyIngestReport()
        )
        monkeypatch.setattr(
            IngestWorker, "_sync_sleep", lambda _self: SleepSyncReport(upserted=0)
        )
        monkeypatch.setattr(
            IngestWorker,
            "_sync_courses",
            lambda _self: CourseSyncReport(summaries_refreshed=3, details_fetched=1),
        )

        outcome = worker._run_cycle(limit=None)

        assert outcome["courses"]["summaries_refreshed"] == 3
        assert "courses_error" not in outcome


class _EmptyIngestReport:
    imported = 0
    skipped = 0
    failed = 0
    activities = 0
    records = 0

    def summary(self) -> str:
        return "0 imported"


class TestServerTools:
    def _seed(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        from garmin_mcp.server import tools

        with duckdb.connect(str(settings.db_path)) as conn:
            course = parse_course_summary(_raw_summary(1, name="Trail du Charvin"))
            write_course_summary(conn, course)
            attach_detail(course, _raw_detail(1, num_points=10))
            write_course_detail(conn, course)

            no_geometry = parse_course_summary(_raw_summary(2, name="Not yet synced"))
            write_course_summary(conn, no_geometry)
        monkeypatch.setattr(tools, "_settings", lambda: settings)

    def test_list_courses_reports_geometry_status(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        self._seed(db, monkeypatch)
        result = tools.list_courses()

        assert result["count"] == 2
        by_name = {c["name"]: c for c in result["courses"]}
        assert by_name["Trail du Charvin"]["geometry_synced"] is True
        assert by_name["Not yet synced"]["geometry_synced"] is False

    def test_get_course_profile_returns_the_profile_and_waypoints(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        self._seed(db, monkeypatch)
        result = tools.get_course_profile(1, max_points=5)

        assert result["points_total"] == 10
        assert len(result["profile"]["distance_km"]) <= 5
        assert result["waypoints"][0]["name"] == "Ravitaillement"

    def test_a_course_with_no_geometry_yet_says_so(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        self._seed(db, monkeypatch)
        result = tools.get_course_profile(2)

        assert "profile" not in result
        assert "note" in result

    def test_an_unknown_course_id_raises(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        self._seed(db, monkeypatch)
        with pytest.raises(ValueError, match="not found"):
            tools.get_course_profile(999)


def _write_course_with_profile(
    settings: Settings,
    course_id: int,
    *,
    distance_m: float,
    points: list[tuple[float, float]],  # (distance_m, elevation_m)
    waypoints: list[tuple[str, float]] = (),  # (name, distance_m)  # type: ignore[assignment]
) -> None:
    """Build a course with a controlled profile, bypassing the parser.

    Used only by the nutrition-plan tests, which need to know the exact
    elevation gain a segment should compute to, not just that parsing round-
    trips correctly (already covered by TestCourseParser).
    """
    from garmin_mcp.domain.models import Course, CourseTrackPoint, CourseWaypoint

    course = Course(course_id=course_id, name=f"Course {course_id}", distance_m=distance_m)
    course.track = [
        CourseTrackPoint(point_index=i, elevation_m=elevation, distance_m=distance)
        for i, (distance, elevation) in enumerate(points)
    ]
    course.waypoints = [
        CourseWaypoint(waypoint_index=i, name=name, distance_m=distance)
        for i, (name, distance) in enumerate(waypoints)
    ]
    with duckdb.connect(str(settings.db_path)) as conn:
        write_course_summary(conn, course)
        write_course_detail(conn, course)


class TestNutritionPlan:
    def test_splits_at_named_waypoints_and_sizes_by_effort(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        # 0-3km: +100m climb. 3-6km: flat. A pure-distance split would give
        # both segments equal time; the climb must pull more of it.
        _write_course_with_profile(
            db,
            1,
            distance_m=6000.0,
            points=[(0, 1000), (1000, 1033), (2000, 1066), (3000, 1100), (6000, 1100)],
            waypoints=[("Ravito 1", 3000.0)],
        )
        monkeypatch.setattr(tools, "_settings", lambda: db)

        result = tools.plan_nutrition(1, estimated_duration_min=60, weight_kg=78)

        assert len(result["segments"]) == 2
        climb, flat = result["segments"]
        assert climb["aid_station"] == "Ravito 1"
        # effort: climb = 3000 + 100*10 = 4000; flat = 3000 + 0 = 3000
        assert climb["duration_min"] > flat["duration_min"]
        assert climb["duration_min"] + flat["duration_min"] == pytest.approx(60, abs=1)

    def test_falls_back_to_even_chunks_without_waypoints(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        _write_course_with_profile(
            db, 1, distance_m=12000.0, points=[(0, 1000), (12000, 1000)], waypoints=[]
        )
        monkeypatch.setattr(tools, "_settings", lambda: db)

        result = tools.plan_nutrition(1, estimated_duration_min=60)

        assert len(result["segments"]) > 1
        assert all("aid_station" not in s for s in result["segments"])

    def test_fluid_rate_defaults_from_weight(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        _write_course_with_profile(db, 1, distance_m=5000.0, points=[(0, 1000), (5000, 1000)])
        monkeypatch.setattr(tools, "_settings", lambda: db)

        result = tools.plan_nutrition(1, estimated_duration_min=60, weight_kg=100)

        assert result["rates_used"]["fluid_ml_per_hour"] == 700  # 100kg * 7ml/kg/h

    def test_fluid_rate_falls_back_to_flat_default_without_weight(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        _write_course_with_profile(db, 1, distance_m=5000.0, points=[(0, 1000), (5000, 1000)])
        monkeypatch.setattr(tools, "_settings", lambda: db)

        result = tools.plan_nutrition(1, estimated_duration_min=60)

        assert result["rates_used"]["fluid_ml_per_hour"] == 550

    def test_explicit_fluid_rate_overrides_weight(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        _write_course_with_profile(db, 1, distance_m=5000.0, points=[(0, 1000), (5000, 1000)])
        monkeypatch.setattr(tools, "_settings", lambda: db)

        result = tools.plan_nutrition(
            1, estimated_duration_min=60, weight_kg=78, fluid_ml_per_hour=900
        )

        assert result["rates_used"]["fluid_ml_per_hour"] == 900

    def test_totals_scale_with_duration(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        _write_course_with_profile(db, 1, distance_m=5000.0, points=[(0, 1000), (5000, 1000)])
        monkeypatch.setattr(tools, "_settings", lambda: db)

        one_hour = tools.plan_nutrition(1, estimated_duration_min=60)
        two_hours = tools.plan_nutrition(1, estimated_duration_min=120)

        expected = 2 * one_hour["totals"]["carbs_g"]
        assert two_hours["totals"]["carbs_g"] == pytest.approx(expected, abs=1)

    def test_flags_a_long_stretch_with_no_aid_station(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        # One waypoint near the start; the rest of a long course is unsupported.
        _write_course_with_profile(
            db,
            1,
            distance_m=20000.0,
            points=[(0, 1000), (20000, 1200)],
            waypoints=[("Ravito", 1000.0)],
        )
        monkeypatch.setattr(tools, "_settings", lambda: db)

        result = tools.plan_nutrition(1, estimated_duration_min=180)

        long_segment = result["segments"][-1]
        assert "unsupported" in long_segment

    def test_a_short_segment_is_not_flagged_unsupported(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        _write_course_with_profile(
            db,
            1,
            distance_m=5000.0,
            points=[(0, 1000), (5000, 1000)],
            waypoints=[("Ravito", 4900.0)],
        )
        monkeypatch.setattr(tools, "_settings", lambda: db)

        result = tools.plan_nutrition(1, estimated_duration_min=30)

        assert "unsupported" not in result["segments"][-1]

    def test_rejects_a_nonpositive_duration(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        _write_course_with_profile(db, 1, distance_m=5000.0, points=[(0, 1000), (5000, 1000)])
        monkeypatch.setattr(tools, "_settings", lambda: db)

        with pytest.raises(ValueError, match="positive"):
            tools.plan_nutrition(1, estimated_duration_min=0)

    def test_unknown_course_raises(self, db: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        from garmin_mcp.server import tools

        monkeypatch.setattr(tools, "_settings", lambda: db)
        with pytest.raises(ValueError, match="not found"):
            tools.plan_nutrition(999, estimated_duration_min=60)

    def test_course_without_geometry_raises(
        self, db: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from garmin_mcp.server import tools

        with duckdb.connect(str(db.db_path)) as conn:
            write_course_summary(conn, parse_course_summary(_raw_summary(1)))
        monkeypatch.setattr(tools, "_settings", lambda: db)

        with pytest.raises(ValueError, match="no geometry"):
            tools.plan_nutrition(1, estimated_duration_min=60)

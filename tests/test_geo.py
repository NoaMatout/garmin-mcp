"""Tests for the pure geometry and GPX code.

No database, no Garmin: coordinates in, coordinates out. The properties that
matter are the ones a map or a GPX consumer would notice if they broke — a
track bridged across a gap, a loop collapsed into one lap, an altitude
invented for a point that had none.
"""

from __future__ import annotations

import math
import random
import time
import xml.etree.ElementTree as ET
from itertools import pairwise

import pytest

from garmin_mcp.domain import gpx
from garmin_mcp.domain.geo import (
    EARTH_RADIUS_M,
    GAP_THRESHOLD_S,
    TrackPoint,
    bounding_box,
    haversine_m,
    is_valid_fix,
    simplify_segments,
    split_segments,
)

NS = {"g": "http://www.topografix.com/GPX/1/1"}


def _pt(t: float, lat: float, lon: float, alt: float | None = None, dist: float | None = None):
    return TrackPoint(lat=lat, lon=lon, elapsed_s=t, altitude_m=alt, distance_m=dist)


class TestHaversine:
    def test_one_degree_of_latitude_is_about_111_km(self) -> None:
        assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, abs=60)

    def test_is_symmetric_and_zero_on_itself(self) -> None:
        assert haversine_m(49.2, 4.0, 49.2, 4.0) == 0
        assert haversine_m(49.2, 4.0, 49.3, 4.1) == pytest.approx(haversine_m(49.3, 4.1, 49.2, 4.0))

    def test_longitude_degrees_shrink_with_latitude(self) -> None:
        equator = haversine_m(0, 0, 0, 1)
        reims = haversine_m(49.25, 4, 49.25, 5)
        assert reims == pytest.approx(equator * math.cos(math.radians(49.25)), rel=0.01)


class TestValidFix:
    @pytest.mark.parametrize(
        ("lat", "lon"), [(49.2, 4.0), (-33.9, 151.2), (89.9, 179.9), (0.0, 1.0), (1.0, 0.0)]
    )
    def test_real_coordinates_are_valid(self, lat: float, lon: float) -> None:
        assert is_valid_fix(lat, lon)

    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(None, 4.0), (49.2, None), (91.0, 4.0), (49.2, 181.0), (0.0, 0.0)],
    )
    def test_missing_out_of_range_and_null_island_are_not(self, lat, lon) -> None:  # type: ignore[no-untyped-def]
        assert not is_valid_fix(lat, lon)


class TestBoundingBox:
    def test_contains_every_point_inside_the_radius(self) -> None:
        lat, lon, radius = 49.25, 4.03, 10_000.0
        lat_min, lat_max, lon_min, lon_max = bounding_box(lat, lon, radius)
        assert lon_min is not None and lon_max is not None
        for bearing in range(0, 360, 15):
            d_lat = math.degrees(radius * 0.999 / EARTH_RADIUS_M) * math.cos(math.radians(bearing))
            d_lon = (
                math.degrees(radius * 0.999 / EARTH_RADIUS_M)
                * math.sin(math.radians(bearing))
                / math.cos(math.radians(lat))
            )
            assert lat_min <= lat + d_lat <= lat_max
            assert lon_min <= lon + d_lon <= lon_max

    def test_gives_up_on_longitude_across_the_antimeridian(self) -> None:
        assert bounding_box(10.0, 179.99, 20_000.0)[2:] == (None, None)

    def test_gives_up_on_longitude_near_a_pole(self) -> None:
        assert bounding_box(89.99, 10.0, 50_000.0)[2:] == (None, None)


class TestSplitSegments:
    def test_a_continuous_track_is_one_segment(self) -> None:
        points = [_pt(t, 49.0 + t * 1e-4, 4.0) for t in range(10)]
        segments, interruptions = split_segments(points)
        assert len(segments) == 1 and not interruptions

    def test_a_hole_longer_than_the_threshold_splits_the_track(self) -> None:
        points = [_pt(t, 49.0, 4.0) for t in range(5)] + [
            _pt(t, 49.1, 4.1) for t in range(5 + 300, 10 + 300)
        ]
        segments, interruptions = split_segments(points)
        assert [len(s) for s in segments] == [5, 5]
        assert interruptions[0].gap_s == pytest.approx(301 - 0)
        assert interruptions[0].after_elapsed_s == 4

    def test_a_hole_exactly_at_the_threshold_does_not_split(self) -> None:
        points = [_pt(0, 49.0, 4.0), _pt(GAP_THRESHOLD_S, 49.0, 4.0001)]
        assert len(split_segments(points)[0]) == 1

    def test_device_distance_across_the_gap_is_reported_when_known(self) -> None:
        points = [_pt(0, 49, 4, dist=100.0), _pt(500, 49.1, 4, dist=100.0)]
        assert split_segments(points)[1][0].gap_distance_m == 0.0

    def test_distance_across_the_gap_is_none_when_either_end_lacks_it(self) -> None:
        points = [_pt(0, 49, 4, dist=None), _pt(500, 49.1, 4, dist=100.0)]
        assert split_segments(points)[1][0].gap_distance_m is None

    def test_a_distance_counter_that_goes_backwards_is_not_reported(self) -> None:
        points = [_pt(0, 49, 4, dist=900.0), _pt(500, 49.1, 4, dist=10.0)]
        assert split_segments(points)[1][0].gap_distance_m is None

    def test_no_points_no_segments(self) -> None:
        assert split_segments([]) == ([], [])


class TestSimplify:
    def test_the_budget_is_met_exactly(self) -> None:
        rng = random.Random(1)
        track = [
            _pt(t, 49 + t * 1e-4 + rng.random() * 1e-4, 4 + rng.random() * 1e-3)
            for t in range(1000)
        ]
        result = simplify_segments([track], 100)
        assert result.points_kept == 100
        assert len(result.segments[0]) == 100

    def test_a_track_within_budget_is_returned_untouched(self) -> None:
        track = [_pt(t, 49 + t * 1e-4, 4) for t in range(20)]
        result = simplify_segments([track], 50)
        assert result.segments == [track]
        assert result.max_deviation_m == 0.0

    def test_ends_are_always_kept_and_order_is_preserved(self) -> None:
        rng = random.Random(2)
        track = [_pt(t, 49 + rng.random() * 1e-3, 4 + rng.random() * 1e-3) for t in range(500)]
        kept = simplify_segments([track], 20).segments[0]
        assert kept[0] is track[0] and kept[-1] is track[-1]
        assert [p.elapsed_s for p in kept] == sorted(p.elapsed_s for p in kept)

    def test_collinear_points_collapse_to_the_ends(self) -> None:
        track = [_pt(t, 49 + t * 1e-4, 4.0) for t in range(100)]
        result = simplify_segments([track], 10)
        assert result.points_kept == 2
        assert result.max_deviation_m < 0.01

    def test_the_reported_deviation_bounds_every_dropped_point(self) -> None:
        """The promise made to callers: nothing dropped is farther than this."""
        rng = random.Random(3)
        track = [
            _pt(t, 49 + math.sin(t / 30) * 2e-3 + rng.random() * 2e-4, 4 + t * 1e-4)
            for t in range(600)
        ]
        result = simplify_segments([track], 40)
        kept = result.segments[0]
        indices = [track.index(p) for p in kept]
        k = EARTH_RADIUS_M * math.pi / 180
        kx = k * math.cos(math.radians(49))

        def xy(p: TrackPoint) -> tuple[float, float]:
            return p.lon * kx, p.lat * k

        worst = 0.0
        for lo, hi in pairwise(indices):
            ax, ay = xy(track[lo])
            bx, by = xy(track[hi])
            for p in track[lo + 1 : hi]:
                px, py = xy(p)
                dx, dy = bx - ax, by - ay
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
                worst = max(worst, math.hypot(px - ax - t * dx, py - ay - t * dy))
        # The test projects around 49.0 N, the code around the track's mean: allow that.
        assert worst <= result.max_deviation_m * 1.001 + 1e-6

    def test_segments_stay_separate_and_each_keeps_its_ends(self) -> None:
        a = [_pt(t, 49 + t * 1e-4, 4 + (t % 3) * 1e-4) for t in range(200)]
        b = [_pt(1000 + t, 49.5 + t * 1e-4, 4 + (t % 3) * 1e-4) for t in range(200)]
        result = simplify_segments([a, b], 30)
        assert len(result.segments) == 2
        assert result.segments[0][0] is a[0] and result.segments[0][-1] is a[-1]
        assert result.segments[1][0] is b[0] and result.segments[1][-1] is b[-1]

    def test_a_budget_below_the_minimum_is_raised_not_a_gap_dropped(self) -> None:
        segments = [
            [_pt(t, 49 + t * 1e-3, 4 + (t % 2) * 1e-3) for t in range(50)] for _ in range(3)
        ]
        result = simplify_segments(segments, 2)
        assert len(result.segments) == 3
        assert all(len(s) >= 2 for s in result.segments)

    def test_a_repeated_loop_keeps_both_passes(self) -> None:
        """Same coordinates twice is two passes, not one duplicate to remove."""
        n = 80
        circle = [
            _pt(
                i * 5,
                49.0 + 3e-3 * math.sin(2 * math.pi * (i % n) / n),
                4.0 + 3e-3 * math.cos(2 * math.pi * (i % n) / n),
            )
            for i in range(2 * n + 1)
        ]
        kept = simplify_segments([circle], 30).segments[0]
        first_lap = [p for p in kept if p.elapsed_s < n * 5]
        second_lap = [p for p in kept if p.elapsed_s >= n * 5]
        assert len(first_lap) >= 8 and len(second_lap) >= 8

    def test_an_out_and_back_keeps_the_turnaround(self) -> None:
        out = [_pt(t, 49 + t * 1e-4, 4 + (t % 2) * 1e-5) for t in range(100)]
        back = [_pt(100 + t, 49 + (99 - t) * 1e-4, 4 + (t % 2) * 1e-5) for t in range(100)]
        kept = simplify_segments([out + back], 6).segments[0]
        assert any(abs(p.lat - (49 + 99e-4)) < 1e-9 for p in kept)

    def test_fifty_thousand_points_simplify_in_reasonable_time(self) -> None:
        rng = random.Random(4)
        track = [_pt(t, 49 + rng.random() * 0.05, 4 + rng.random() * 0.05) for t in range(50_000)]
        started = time.perf_counter()
        result = simplify_segments([track], 500)
        assert result.points_kept == 500
        assert time.perf_counter() - started < 5


class TestGpx:
    def _parse(self, text: str) -> ET.Element:
        return ET.fromstring(text)

    def test_one_trkseg_per_segment(self) -> None:
        text = gpx.build_gpx("Run", [[_pt(0, 49, 4), _pt(1, 49.1, 4)], [_pt(500, 50, 5)]])
        assert len(self._parse(text).findall(".//g:trkseg", NS)) == 2

    def test_every_point_is_written_in_order_with_its_coordinates(self) -> None:
        seg = [_pt(t, 49 + t * 1e-3, 4 - t * 1e-3) for t in range(5)]
        points = self._parse(gpx.build_gpx("Run", [seg])).findall(".//g:trkpt", NS)
        assert len(points) == 5
        assert [float(p.get("lat")) for p in points] == pytest.approx([p.lat for p in seg])
        assert [float(p.get("lon")) for p in points] == pytest.approx([p.lon for p in seg])

    def test_altitude_is_written_only_where_it_exists(self) -> None:
        seg = [_pt(0, 49, 4, alt=100.0), _pt(1, 49.1, 4, alt=None)]
        points = self._parse(gpx.build_gpx("Run", [seg])).findall(".//g:trkpt", NS)
        assert points[0].find("g:ele", NS) is not None
        assert points[1].find("g:ele", NS) is None  # not 0, not copied from a neighbour

    def test_times_appear_only_when_known(self) -> None:
        stamped = TrackPoint(49, 4, 0, time_iso="2026-09-01T07:00:00Z")
        points = self._parse(gpx.build_gpx("Run", [[stamped, _pt(1, 49.1, 4)]])).findall(
            ".//g:trkpt", NS
        )
        assert points[0].findtext("g:time", namespaces=NS) == "2026-09-01T07:00:00Z"
        assert points[1].find("g:time", NS) is None

    def test_names_are_escaped(self) -> None:
        text = gpx.build_gpx("Trail <Sacy> & co", [[_pt(0, 49, 4)]], description="a < b")
        root = self._parse(text)  # would raise if the XML were malformed
        assert root.findtext("g:trk/g:name", namespaces=NS) == "Trail <Sacy> & co"

    def test_empty_segments_are_skipped(self) -> None:
        text = gpx.build_gpx("Run", [[], [_pt(0, 49, 4)]])
        assert len(self._parse(text).findall(".//g:trkseg", NS)) == 1

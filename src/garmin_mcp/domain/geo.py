"""Geometry on GPS tracks: distance, validity, interruptions, simplification.

Pure functions over plain values — no database, no Garmin. Everything the
location tools rely on to be honest about a track lives here so it can be
tested with a handful of synthetic coordinates.

Three rules this module exists to enforce:

* **A fix is valid or it is absent.** Nothing is interpolated, snapped or
  guessed. A missing altitude stays missing.
* **An interruption is never bridged.** When the recording stops and resumes,
  the track is two segments, not one line with a straight stretch invented
  across the gap.
* **Passes are never merged.** An out-and-back or a loop run twice keeps both
  passes: points are identified by their position in the recording, never by
  where they are on the map.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

# Mean Earth radius (IUGG). Good to ~0.3 % anywhere on the globe, which is far
# inside GPS noise for the distances a "local activity" search deals in.
EARTH_RADIUS_M = 6_371_008.8

# A hole in the recording longer than this splits the track. 60 s is well above
# the 1-4 s a watch records at (smart recording included) and well below any
# real stop, so it separates "the watch was paused or lost the sky" from "this
# sample simply arrived late". Reported back to the caller, never hidden.
GAP_THRESHOLD_S = 60.0

# Below this a point is on the line as far as anyone can tell (one millimetre,
# well under floating-point noise at these magnitudes). Without a floor, a
# perfectly straight track keeps "improving" by 1e-14 m until the budget is spent.
_NEGLIGIBLE_DEVIATION_M = 1e-3


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points.

    This is a straight line over the Earth's surface. It says nothing about
    roads, paths or how long it takes to get from one point to the other.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def is_valid_fix(lat: float | None, lon: float | None) -> bool:
    """Whether a coordinate pair is a real GPS fix.

    Rejects missing values, out-of-range values, and exactly (0, 0): watches
    write it before they have acquired the sky, and no trail in the product's
    scope runs through the Gulf of Guinea.
    """
    if lat is None or lon is None:
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    return not (lat == 0.0 and lon == 0.0)


def bounding_box(
    lat: float, lon: float, radius_m: float
) -> tuple[float, float, float | None, float | None]:
    """A cheap rectangle that contains every point within `radius_m`.

    A pre-filter only: the exact great-circle test always runs after it, so a
    box that is too generous costs time and never correctness. Longitude
    bounds come back as None when the box touches a pole or crosses the
    antimeridian, where a plain min/max comparison would be wrong.
    """
    d_lat = math.degrees(radius_m / EARTH_RADIUS_M)
    lat_min, lat_max = lat - d_lat, lat + d_lat
    if lat_min <= -90.0 or lat_max >= 90.0:
        return max(lat_min, -90.0), min(lat_max, 90.0), None, None

    d_lon = math.degrees(radius_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    lon_min, lon_max = lon - d_lon, lon + d_lon
    if lon_min < -180.0 or lon_max > 180.0:
        return lat_min, lat_max, None, None
    return lat_min, lat_max, lon_min, lon_max


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """One valid GPS fix, with whatever else the device recorded at that instant."""

    lat: float
    lon: float
    elapsed_s: float
    altitude_m: float | None = None
    distance_m: float | None = None  # cumulative, as the device reported it
    time_iso: str | None = None  # UTC, "2026-10-17T08:12:03Z"


@dataclass(frozen=True, slots=True)
class Interruption:
    """A hole between two consecutive fixes that the track does not bridge."""

    after_elapsed_s: float  # elapsed time of the last fix before the hole
    gap_s: float
    # Device-reported distance covered across the hole, when both ends have one.
    # A pause reads ~0; a lost signal during which the watch kept counting does
    # not. Offered as evidence, not as a verdict on the cause.
    gap_distance_m: float | None


def split_segments(
    points: Sequence[TrackPoint], gap_threshold_s: float = GAP_THRESHOLD_S
) -> tuple[list[list[TrackPoint]], list[Interruption]]:
    """Cut a track wherever the recording has a hole longer than the threshold."""
    if not points:
        return [], []

    segments: list[list[TrackPoint]] = [[points[0]]]
    interruptions: list[Interruption] = []
    for previous, current in pairwise(points):
        gap = current.elapsed_s - previous.elapsed_s
        if gap > gap_threshold_s:
            covered = (
                current.distance_m - previous.distance_m
                if current.distance_m is not None and previous.distance_m is not None
                else None
            )
            if covered is not None and covered < 0:
                covered = None  # a counter that went backwards is not evidence of anything
            interruptions.append(Interruption(previous.elapsed_s, gap, covered))
            segments.append([])
        segments[-1].append(current)
    return segments, interruptions


@dataclass(frozen=True, slots=True)
class Simplified:
    segments: list[list[TrackPoint]]
    points_kept: int
    # Upper bound on how far any dropped point lies from the simplified line.
    # Exact for the algorithm, so it can be quoted to the caller as a promise.
    max_deviation_m: float


def simplify_segments(segments: Sequence[Sequence[TrackPoint]], max_points: int) -> Simplified:
    """Reduce a track to at most `max_points` while keeping its shape.

    Ranked Douglas-Peucker: always split the interval whose farthest point
    deviates most from its chord, keep that point, repeat until the budget is
    spent. Unlike picking a tolerance and hoping, this lands on the requested
    size exactly and tells you the worst error it accepted.

    The ends of every segment are always kept, so an interruption is still an
    interruption after simplification. When the budget is smaller than that
    minimum it is raised to it rather than dropping the gaps.
    """
    total = sum(len(s) for s in segments)
    floor = sum(min(len(s), 2) for s in segments)
    budget = max(max_points, floor)
    if total <= budget:
        return Simplified([list(s) for s in segments], total, 0.0)

    # Planar projection around the track's centre: at trail scale the error of
    # an equirectangular approximation is metres, and this is only ranking
    # points by deviation, not measuring anything reported.
    all_lat = [p.lat for s in segments for p in s]
    lat0 = math.radians(sum(all_lat) / len(all_lat))
    k = EARTH_RADIUS_M * math.pi / 180.0
    kx = k * math.cos(lat0)

    xs: list[list[float]] = [[p.lon * kx for p in s] for s in segments]
    ys: list[list[float]] = [[p.lat * k for p in s] for s in segments]

    keep: list[set[int]] = [{0, len(s) - 1} if s else set() for s in segments]
    kept = sum(len(ends) for ends in keep)

    # Heap of (-deviation, segment, lo, hi, farthest index). Largest deviation first.
    heap: list[tuple[float, int, int, int, int]] = []

    def push(seg: int, lo: int, hi: int) -> None:
        if hi - lo < 2:
            return
        dev, idx = _farthest(xs[seg], ys[seg], lo, hi)
        heapq.heappush(heap, (-dev, seg, lo, hi, idx))

    for seg, s in enumerate(segments):
        if len(s) > 2:
            push(seg, 0, len(s) - 1)

    while heap and kept < budget:
        neg_dev, seg, lo, hi, idx = heapq.heappop(heap)
        if -neg_dev < _NEGLIGIBLE_DEVIATION_M:
            heapq.heappush(heap, (neg_dev, seg, lo, hi, idx))
            break  # everything left is collinear: nothing to gain
        keep[seg].add(idx)
        kept += 1
        push(seg, lo, idx)
        push(seg, idx, hi)

    max_dev = -heap[0][0] if heap else 0.0
    simplified = [[s[i] for i in sorted(keep[n])] for n, s in enumerate(segments)]
    return Simplified(simplified, kept, max_dev)


def _farthest(xs: list[float], ys: list[float], lo: int, hi: int) -> tuple[float, int]:
    """The point strictly between lo and hi farthest from the lo-hi chord."""
    ax, ay = xs[lo], ys[lo]
    dx, dy = xs[hi] - ax, ys[hi] - ay
    length_sq = dx * dx + dy * dy

    best, best_idx = -1.0, lo + 1
    for i in range(lo + 1, hi):
        px, py = xs[i] - ax, ys[i] - ay
        if length_sq < 1e-9:
            # A closed loop: the chord has no length, so distance is to the
            # shared end point. Without this a loop would collapse to nothing.
            dist_sq = px * px + py * py
        else:
            t = max(0.0, min(1.0, (px * dx + py * dy) / length_sq))
            ex, ey = px - t * dx, py - t * dy
            dist_sq = ex * ex + ey * ey
        if dist_sq > best:
            best, best_idx = dist_sq, i
    return math.sqrt(best), best_idx

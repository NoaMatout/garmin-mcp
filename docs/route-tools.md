# Finding past runs by place, and reading their tracks

Two tools and two MCP resources. All read-only, all answered from the local
database: they keep working when Garmin is unreachable and when the Garmin
session has expired. They make no network call.

> A **recorded activity** shows where someone ran once. It is not a validated or
> public route, and says nothing about trail access rights, technicality, water
> points, or whether the path still exists. Every response says so.

All coordinates in this document are synthetic.

## `find_local_activities`

Recorded runs that **started** near a point.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `latitude`, `longitude` | float | required | WGS84 decimal degrees |
| `radius_km` | float | 10 | 0 < r ≤ 300 |
| `activity_type` | string \| null | `"running"` | FIT sport. Trail running is `running` with sub-sport `trail`. `null` = every sport |
| `sub_sport` | string \| null | null | e.g. `"trail"` |
| `min_distance_km`, `max_distance_km` | float | – | distance range, km |
| `min_ascent_m`, `max_ascent_m` | float | – | positive elevation gain (D+), metres |
| `min_duration_min`, `max_duration_min` | float | – | minutes, on the time chosen below |
| `duration_basis` | `"moving"` \| `"elapsed"` | `"moving"` | which stored time the duration range applies to |
| `sort` | `"nearest"` \| `"recent"` | `"nearest"` | |
| `limit` / `offset` | int | 20 / 0 | page size (max 100) and skip |

### What the radius means

A **great-circle (straight-line) distance from your point to the activity's
recorded start point**. Not to any other point of the track, and not a road
distance or travel time. A run that starts 30 km away and passes through the
circle is not returned; a run that starts inside and wanders out is. The response
repeats this in `query.radius_means`, and `start_point_distance_km` is that same
straight line.

### Measures and their provenance

`measures` are the values the device wrote in the FIT **session summary**,
reported as recorded. Nothing is recomputed from the GPS samples and nothing
silently replaces a summary figure. (A multisport parent is the exception the
source cannot avoid: its figures are the sum of its legs' summaries, and the
entry says `measures_source`.)

- `moving_time_*` is the stopwatch time (paused periods excluded).
  `elapsed_time_*` runs start to finish. Both are returned; they differ on any run
  with stops.
- A measure the device did not record is **omitted**, not zero. A range on an
  omitted measure excludes the activity: a missing value cannot satisfy "at least
  800 m of climb".

### Quality indicators

Computed from the stored per-second samples, for the returned page only.

| Field | Meaning |
|---|---|
| `has_gps` | at least one valid fix is stored (valid = both coordinates present, in range, not exactly 0,0) |
| `has_altitude` | at least one of those fixes has an altitude |
| `samples`, `gps_points` | stored samples, and how many carry a valid fix |
| `gps_coverage`, `altitude_coverage` | `gps_points / samples`, and altitude over fixes |
| `interruptions`, `longest_gap_s` | holes between consecutive fixes longer than `gap_threshold_s` (60 s) |
| `partial_track` | true when any of: coverage < 95 %, an interruption, first fix > 60 s after start, no fix in the last 60 s. `null` when there are no samples to judge |
| `partial_reasons` | the reasons, in words, with the numbers |

An activity with no stored samples (a summary-only import) reports
`has_gps: false` and `partial_track: null` — unknowable, not "complete".

### Pagination

`total_matches` is the count before paging. `has_more` / `next_offset` say how to
continue. An `offset` past the end returns an empty page and still reports the
total.

### Example

Request:

```json
{"latitude": 49.2583, "longitude": 4.0317, "radius_km": 10, "min_distance_km": 10, "limit": 5}
```

Response (one of two activities shown):

```json
{
  "query": {
    "radius_km": 10,
    "radius_means": "straight-line distance from the search point to each activity's START point; not the whole track, not road or travel time",
    "activity_type": "running", "sub_sport": null, "duration_basis": "moving", "sort": "nearest"
  },
  "total_matches": 2, "returned": 2, "offset": 0, "has_more": false,
  "activities": [
    {
      "activity_id": 102,
      "name": "Sortie avec pause",
      "sport": "running", "sub_sport": "trail",
      "start_local": "2026-09-03T09:00",
      "start": {"lat": 49.24031, "lon": 4.05925},
      "start_point_distance_km": 2.83,
      "measures": {
        "distance_km": 24.0, "descent_m": 290,
        "moving_time_s": 8000, "moving_time": "2:13:20",
        "elapsed_time_s": 9200, "elapsed_time": "2:33:20"
      },
      "quality": {
        "has_gps": true, "has_altitude": true,
        "samples": 121, "gps_points": 121, "gps_coverage": 1.0, "altitude_coverage": 1.0,
        "interruptions": 1, "longest_gap_s": 785.0,
        "partial_track": true,
        "partial_reasons": ["1 recording gap(s) longer than 60 s"]
      }
    }
  ],
  "measures_source": "FIT session summary, as written by the device; not recomputed",
  "gap_threshold_s": 60.0
}
```

`ascent_m` is absent on that entry: the synthetic device did not record one.

## `get_activity_route`

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `activity_id` | int | required | from `find_local_activities` or `list_activities` |
| `max_points` | int | 500 | bound on returned points (2 … 2000) |

### Where the geometry comes from

The per-sample GPS data kept at ingestion (`records`), one point per recorded
sample. **Not** `get_activity_streams`, which averages into buckets for the
conversation and would move the athlete off the path.

### Simplification

For a map the track is simplified to at most `max_points` by ranked
Douglas–Peucker (always keep the point that deviates most from the current
line). `track.simplification` states `points_available`, `points_returned`,
whether it was simplified, and **`max_deviation_m`: the largest distance any
dropped point lies from the returned line** — a bound, not an estimate. Segment
ends are always kept. If `max_points` is smaller than the number of segment ends,
it is raised to that minimum rather than dropping a gap.

Output is columnar per segment (`lat`, `lon`, `elapsed_s`, and `altitude_m` /
`distance_km` when any point has them), about a third of the tokens of a list of
objects. Coordinates are rounded to 5 decimals (≈ 1 m).

### Interruptions are never bridged

Where the recording has a hole longer than `gap_threshold_s` (a pause, a lost
signal) the track becomes **separate segments** and the hole is listed under
`interruptions` (`gap_s`, and the device's own distance counter across it when
both ends have one — evidence, not a verdict on the cause). A consumer drawing it
must not join the segments. The GPX does the same with one `<trkseg>` each.

### Nothing is invented

An absent altitude stays absent (`null` in the column, no `<ele>` in the GPX). A
missing cumulative distance is `null`. A distance counter that goes backwards is
not reported. `missing_data` lists what is absent, in words. Loops and
out-and-backs are never deduplicated: points are identified by their position in
the recording, so a loop run twice keeps both laps and the cumulative distance
keeps increasing.

### The GPX

`gpx.resource_uri` (`garmin://activity/{id}/route.gpx`) is an **MCP resource**:
read it with a `resources/read` request on the same connection. It contains
**every valid fix, unsimplified**; `gpx.bytes` and `gpx.sha256` let a client check
what it received.

- **Transport:** works identically over stdio and streamable HTTP. No client needs
  to share a filesystem with the server, and no path is ever returned.
- **Availability:** generated on demand from the stored samples. There is no file,
  no expiry and nothing to clean up. It is available whenever the activity is in
  the database and the server is reachable, and it reflects the data as stored
  *now* (a re-parse that changes samples changes the hash).
- **Size:** about 105 bytes per point; a 56 km race at 1 Hz is around 3–4 MB. A
  recording above 150 000 fixes is refused (`route_unavailable`).
- **A local file** (stdio, same machine): `garmin-mcp export-gpx <activity_id> [-o file] [--force]`.
- A saved course is available the same way at `garmin://course/{id}/route.gpx`
  (its stored geometry, one segment, no timestamps).

### Example

Request: `{"activity_id": 102, "max_points": 60}`. Response, arrays shortened:

```json
{
  "activity_id": 102,
  "summary": {"distance_km": 24.0, "moving_time": "2:13:20", "elapsed_time": "2:33:20",
              "source": "FIT session summary"},
  "quality": {"has_gps": true, "interruptions": 1, "partial_track": true,
              "partial_reasons": ["1 recording gap(s) longer than 60 s"]},
  "gap_threshold_s": 60.0,
  "notes": ["A recorded activity shows where someone ran once. It is not a validated or public route, and says nothing about trail access rights, technicality, water points or whether the path still exists."],
  "track": {
    "simplification": {
      "method": "ranked Douglas-Peucker (planar, per segment; segment ends always kept)",
      "points_available": 121, "points_returned": 60,
      "simplified": true, "max_deviation_m": 5.5
    },
    "bounds": {"min_lat": 49.22233, "max_lat": 49.2583, "min_lon": 4.00415, "max_lon": 4.05925},
    "segments": [
      {"lat": [49.24031, 49.24219, "…"], "lon": [4.05925, 4.0591, "…"],
       "elapsed_s": [0, 10, "…"], "altitude_m": [170.0, 172.0, "…"],
       "distance_km": [0.0, 0.016, "…"]},
      {"lat": [49.24031, 49.23843, "…"], "lon": [4.00415, 4.0043, "…"],
       "elapsed_s": [1080, 1090, "…"], "altitude_m": [170.0, 172.0, "…"],
       "distance_km": [0.48, 0.496, "…"]}
    ]
  },
  "interruptions": [{"after_elapsed_s": 295, "gap_s": 785, "device_distance_during_gap_m": 8}],
  "missing_data": ["1 recording gap(s): segments are not joined"],
  "gpx": {
    "resource_uri": "garmin://activity/102/route.gpx",
    "mime_type": "application/gpx+xml",
    "points": 121, "segments": 2, "bytes": 13138,
    "sha256": "ba49324b…e74",
    "availability": "generated on demand from the stored samples; no expiry, no file written; read it with MCP resources/read"
  }
}
```

An activity with no valid fix returns `track: null` and `gpx: null` with a note.

## Units and limits

| | |
|---|---|
| Distance | kilometres in `*_km`, metres in `*_m`; cumulative distance is the device's |
| Time | seconds in `*_s`, `H:MM:SS` in the unsuffixed string |
| Coordinates | WGS84 decimal degrees, 5 decimals in JSON, 7 in GPX |
| Search page | ≤ 100 results; radius ≤ 300 km |
| Route response | ≤ 2000 points |
| Gap threshold | 60 s, reported in every response |
| Track in memory | ≤ 150 000 fixes |

## Errors

Tools raise; the message carries the way forward. Machine-readable codes are in
`GarminMcpError.code` and listed in `capabilities.md`.

| Situation | Result |
|---|---|
| latitude/longitude out of range, radius ≤ 0 or > 300, `min > max`, negative bound, unknown `sort` / `duration_basis` | `ValueError` naming the parameter |
| unknown activity id | `ActivityNotFoundError` (`activity_not_found`) |
| no fix to export, malformed id in a resource URI | `ValueError` |
| unknown course, course without geometry, track above the ceiling | `RouteUnavailableError` (`route_unavailable`) |
| database locked by a sync in progress | retried for ~2 s, then `DatabaseLockedError` (`database_locked`) |

## Privacy

Tracks start at the athlete's home. The server has no authentication of its own
(see `web-app-integration.md`). Logs carry counts and ids, never coordinates; the
test suite asserts it.

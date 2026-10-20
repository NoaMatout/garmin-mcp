# Validation report

What was checked, how, and what was **not** checked. The three categories are kept
apart on purpose.

## 1. Local tests (real code, real DuckDB, synthetic data)

`uv run pytest`: **470 passed** (340 before this work; 130 added), `ruff check`
and `mypy --strict` clean. The new tests sit in `tests/test_geo.py`,
`tests/test_local_routes.py`, `tests/test_capabilities.py`, on fixtures built by
`tests/geo_fixtures.py`. Every coordinate is invented; no FIT file, trace,
database or credential is committed.

| Area | Covered |
|---|---|
| Search radius | inside / outside; **corner of the bounding box** (inside the square, outside the circle); start vs track (a run crossing the centre but starting 30 km away is not found); activity without a start position; antimeridian; boundary values |
| Filters | sport, sub-sport, distance, ascent, duration on **moving vs elapsed** time; a missing measure cannot satisfy a range |
| Pagination | consecutive pages, `has_more`/`next_offset`, page past the end, caps, sort |
| Provenance | measures equal the stored summary even when the track says otherwise |
| Quality | clean; no altitude; recording gap; low coverage; late first fix and dead tail; summary-only; (0, 0) fixes |
| Geometry | exact budget; ends kept; collinear collapse; **deviation bound verified by brute force**; gaps never bridged; **repeated loop keeps both laps**; out-and-back keeps the turnaround; 50 000 points in 0.2 s |
| Missing data | no altitude stays absent (columns, GPX); partial altitude gives nulls; backwards distance counter dropped |
| GPX | one `<trkseg>` per segment; all points, in order; `<ele>`/`<time>` only when known; escaping; hash and size match what the tool advertises; read through the MCP resource path |
| Locality | the route tools and `get_capabilities` run with `socket.connect` forbidden; logs hold no coordinates |
| Capabilities | fresh install; expired session (local reads stay available, Garmin features do not, `last_ok_at` kept); stale data; writes implemented ≠ verified; secrets and paths never appear; Garmin error text never stored |
| Worker | each failure class (auth, blocked, unavailable, unexpected) recorded; a write request recorded; a failing state file never breaks a cycle |
| Existing guarantees | the previous 340 tests, including the two-step confirmation of every write, still pass untouched |

Mutation checks: removing the exact radius test, bridging gaps, and erasing
`last_ok_at` each make tests fail (the first one did **not** at first — it exposed a
missing bounding-box-corner test, now added).

Measured on 2.4 million synthetic samples (600 activities): `find_local_activities`
24–45 ms for a page; `get_activity_route` 80 ms for a 4 000-fix activity.

## 2. Simulated

- Garmin's side of the worker is replaced by exceptions (`GarminAuthError`,
  `GarminBlockedError`, …) to drive the state record. This proves the worker
  *classifies and records* failures correctly; it proves nothing about what Garmin
  actually returns.
- The MCP resource path is exercised in-process (`server.read_resource`), not over
  a socket. Stdio and HTTP use the same handler; neither transport was started.
- `export-gpx` run through Typer's `CliRunner`.

## 3. Verified against Garmin

**Nothing new.** No request was sent to Garmin and no write was attempted on the
account during this work. Statements about Garmin in `garmin-courses.md` come from
reading the installed `garminconnect` 0.3.9 and this repository. The course
listing's behaviour against a real account is documented in `course_reader.py` /
commit `91fbe07` from earlier work and was not re-verified.

## Not done, on purpose

- GPX import as a navigation course (`garmin-courses.md` says why).
- Authentication, multi-user isolation (`web-app-integration.md`).
- A live probe of Garmin from the server: it holds no credentials.
- Any change to the database schema: none was needed, so there is no migration.

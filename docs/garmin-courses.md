# Garmin courses: what the dependencies actually allow

Three different things live in Garmin Connect and are easy to confuse. This
project treats them as three different things.

| Object | What it is | Where it comes from | Handled by |
|---|---|---|---|
| **Activity** | something that was recorded: a FIT file with samples | the watch, or an upload | `ActivitySource`, the ingest pipeline, `activities` / `records` tables |
| **Workout** | a structured *session* (warm-up, 4 × 6 reps…) to be run | the Workouts library | `garmin/workout_writer.py`, `create_workout` and friends |
| **Course** | a *route to navigate*: a line on a map with a profile and named points | the Courses library | `garmin/course_reader.py`, `courses` / `course_points` tables |

Uploading a GPX file to Garmin Connect can create either of the first and last,
depending on which endpoint receives it. That is the whole risk of this topic.

## Diagnosis

Status vocabulary: **available** (implemented and used here), **not verified in
real conditions** (the code path exists, nothing has proven it works against
Garmin), **not supported** (no way to do it with what is installed).

Findings come from reading the installed dependency (`garminconnect` 0.3.9) and
this repository. **No request was sent to Garmin while preparing this document,
and no write was attempted on any account.**

| Capability | Status | Basis |
|---|---|---|
| List the account's courses | **available** | `garmin/course_reader.py::list_courses` → `GET /course-service/course` through `Garmin.connectapi`. Not in `garminconnect`: a search of its sources for `course` and `route` finds no endpoint. The endpoint is undocumented; the module docstring and commit `91fbe07` record that it was confirmed against a real account when it was written. It has not been re-verified for this work. |
| Fetch one course (geometry + named points) | **available** | `course_reader.get_course` → `GET /course-service/course/{id}`, same basis. Stored in `course_points` / `course_waypoints`. |
| Export a stored course as GPX | **available** (local) | built from the stored geometry (`garmin://course/{id}/route.gpx`). No Garmin access involved. |
| Export a recorded activity as GPX | **available** (local) | built from `records` (`garmin://activity/{id}/route.gpx`, `garmin-mcp export-gpx`). `Garmin.download_activity(..., GPX)` exists in `garminconnect` but is not used: our own export keeps interruptions apart and never touches the network. |
| Import a GPX file **as a course** | **not supported** | `garminconnect` has no course method at all (see above). Its `upload_activity` / `import_activity` accept GPX (`ActivityUploadFormat.GPX`, `__init__.py:2765`) but post to `/upload-service/upload` (`__init__.py:535`), the **activity** importer. A GPX sent there becomes a recorded *activity*, not a navigation course. Course creation would need a call to `course-service` with a payload this project has never seen a response to. |
| Import a GPX as a course through `course-service` directly | **not verified in real conditions** — and not implemented | `Garmin.client.post("connectapi", path, …)` exists (it is what `upload_workout` uses), so a call is mechanically possible. But the path, the upload format and the response are unknown: the web client's own call has not been observed here, and guessing would put a malformed object on a real account. |

## Decision

**GPX import as a course is not implemented.** It would depend on an
undocumented endpoint with an unobserved contract, and the instructions for this
work forbid testing a write on the account. Shipping code that can only be
exercised against a live account, and has never been, is a worse state than a
documented gap.

`get_capabilities` reports it as such (`courses.import_gpx`: `implemented:
false`) so a product does not offer it.

**Fallback, which works today:**

1. Export the track as GPX: the `garmin://activity/{id}/route.gpx` MCP resource
   (what a remote client uses), or `garmin-mcp export-gpx <activity_id>` (writes a
   file next to where you run it).
2. In Garmin Connect (web) → *Training → Courses → Import*, choose the file.
   Garmin creates the course and syncs it to the watch like any other.

A recorded activity exported this way is **not a validated route** (see
`route-tools.md`): check the trail is open and passable before sending anyone on
it.

## If import is built later

What it must do before it touches an account, in order. None of this exists yet.

1. **Observe the real contract first.** Capture one manual import in Garmin
   Connect's web client (endpoint, content type, response, the id it returns) and
   record it here with the date. Until then the status stays *not verified*.
2. **Validate locally.** Size ceiling (a few MB), well-formed XML with no external
   entities, WGS84 coordinates in range, at least two valid points, a point
   ceiling, no `(0, 0)` fixes. Report what was kept and what was rejected.
3. **Preview, then confirm against the exact content.** The preview returns a
   SHA-256 of the validated bytes; the confirming call must send that hash, and is
   refused if the content differs. This is stronger than the current `confirm=true`
   used for workouts, which says "yes" without saying to *what*.
4. **Use the existing switch and path.** Behind `GARMIN_ENABLE_WRITES`, routed
   through the ingest worker, which alone holds credentials.
5. **Survive retries.** Derive an idempotency key from the content hash, remember
   it with the returned course id in `.state/`, and answer a repeat with the
   existing id rather than creating a second course.
6. **Say only what is confirmed.** The result is "created in Garmin Connect, id
   N". It is never "sent to the watch": that happens at the next device sync,
   which this project does not observe.

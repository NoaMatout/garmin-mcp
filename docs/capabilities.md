# `get_capabilities`: what works, and what has been proven

One tool, no arguments, **no network call**. It combines this server's
configuration, a quick read of the local database, and what the ingest worker
last *observed* about Garmin — with the date it observed it.

The server never contacts Garmin: it holds no credentials and cannot log in. So
this tool can say "the worker's last sync succeeded eleven minutes ago" or "the
worker's last attempt failed with an expired session, and it last worked on
Tuesday". It cannot say "Garmin is up right now", and it does not pretend to.

An expired or missing Garmin session **never blocks local reads**. Searching
history, reading tracks and exporting GPX use the database only, and this tool
reports them as available in exactly that situation.

Nothing secret is reported: no credential, token, cookie or session path, and no
filesystem path at all (not even the database's). Garmin's own error text is not
stored or returned — only a stable code.

## Implemented is not verified

Every capability carries both:

| Field | Meaning |
|---|---|
| `implemented` | the code exists |
| `enabled` | it is switched on here (writes are off by default) |
| `available_now` | it can run right now: `true`, `false`, or `null` when unknown |
| `needs` | what it depends on |
| `verification.state` | `verified_ok` · `verified_failing` · `not_verified` · `not_applicable` |
| `verification.basis` | what the state rests on |
| `verification.observed_at` / `last_ok_at` | when |

- `verified_ok` — something real succeeded (a sync outcome, a served request), at
  `observed_at`.
- `verified_failing` — the last real attempt failed; `last_ok_at` says when it
  last worked.
- `not_verified` — implemented, never exercised on this installation. A write
  capability stays here until a write has actually succeeded.
- `not_applicable` — a local computation; no Garmin access involved.

## Sections

| Section | Content |
|---|---|
| `local_history` | `readable`, counts, date range, schema version. If unreadable: `reason` = `database_missing` · `database_locked` · `database_unreadable` |
| `ingest_worker` | `running`, `heartbeat_age_s`, the request kinds it `handles` |
| `sync` | `last_attempt_at`, `last_success_at` (the activity sync completed), `data_freshness` = `fresh` · `stale` (no success within two intervals) · `unknown` |
| `garmin_access` | `status`, `verified`, `observed_at`, `last_ok_at`, `error_code`, `session_saved`, `backend` |
| `writes` | whether writes are enabled, and their scope |
| `capabilities` | the list below |
| `issues` | what is wrong, each with `code`, `severity`, `component`, `message`, `recovery` |

`garmin_access.status`: `unknown` (nothing observed yet) · `ok` · `auth_required`
· `blocked` · `rate_limited` · `unavailable` · `failed`. `verified` is true only
for `ok`, which is an observation with a date, not a promise about the next call.

### Capabilities reported

`history.read`, `history.search_by_location`, `history.route_geometry`,
`history.export_gpx` · `sync.activities`, `sync.sleep` · `courses.read`,
`courses.export_gpx`, `courses.import_gpx` (**not implemented**; see
`garmin-courses.md`) · `workouts.read`, `workouts.write` ·
`activities.write_notes`.

## Structured errors

Every error this project raises on purpose has a stable `code` and a recovery
hint, available as data through `GarminMcpError.as_dict()`
(`{"code", "message", "recovery"}`). Branch on the code, not the message.

| Code | Meaning | Recovery |
|---|---|---|
| `garmin_auth_required` | no session, or it expired | `garmin-mcp auth` (interactive, MFA) |
| `garmin_blocked` | Garmin refused the client (TLS fingerprinting) | `GARMIN_BACKEND=playwright`, or import FIT files by hand |
| `garmin_rate_limited` | Garmin is throttling | wait before the next sync |
| `garmin_unavailable` | Garmin unreachable, or an unclassified Garmin error | the next cycle retries |
| `worker_unavailable` | no ingest worker, or one too old for the request | `docker compose up -d ingest` |
| `database_locked` | a sync holds the file | retry in seconds |
| `activity_not_found` | unknown activity id | check with `list_activities` / `find_local_activities` |
| `route_unavailable` | no geometry / unknown course / track too large | `sync_now`, or check the id |
| `fit_parse_failed`, `ingest_failed` | ingestion problems | see worker logs |
| `writes_disabled` | `GARMIN_ENABLE_WRITES` is off | set it and restart the worker |

`issues[].code` additionally uses `database_missing`, `worker_not_running`,
`data_stale`, `courses_sync_failing`, `garmin_failed`.

## Where the observations come from

`ingest/sync_state.py`. The worker writes `data/.state/garmin-state.json` after
every sync cycle and every request it serves; the server reads it. It contains
timestamps, per-component ok/failed flags and error *codes* — nothing else. The
file is advisory: if it is lost or corrupt the status reads `unknown` and the next
cycle starts a new one. A failure never erases the last success, and a component
that was skipped (sleep and courses do not run when the activity sync failed)
keeps its previous observation: not running is not an observation.

A successful request to Garmin (a sync, a listing, a write) refreshes
`garmin_access`. A failed request refreshes it only when its code says something
about access (`garmin_auth_required`, `garmin_blocked`, …): an invalid workout is
not news about Garmin.

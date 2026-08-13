# garmin-mcp

[![CI](https://github.com/NoaMatout/garmin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/NoaMatout/garmin-mcp/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Ask questions about your Garmin training history in plain language, from
Claude. Raw FIT files in, DuckDB out, five typed MCP tools on top.

```
> compare my last two runs

           Trail du Corbier        Treadmill
Distance   16.35 km                8.52 km
Duration   2:41:15                 35:02
Pace       9:52/km                 4:07/km
Avg HR     176 bpm                 165 bpm
Ascent     1264 m                  —
```

No exporting files by hand, no spreadsheets, no third-party service holding
your data. Everything runs on your machine.

---

## Why this exists

Garmin Connect holds years of training data and offers no practical way to ask
it a question. The web UI answers what its designers anticipated; anything else
means exporting CSVs and opening a spreadsheet.

This project pulls the original FIT files, parses them properly, stores them in
a local analytical database, and exposes a small set of typed tools over MCP so
a model can answer questions against real data instead of guessing.

## Architecture

```
        Garmin Connect                     data/inbox/
              │                          (drop FIT files)
              ▼                                 │
   ┌──────────────────────┐                     │
   │   ActivitySource     │  one narrow         │
   ├──────────┬───────────┤  interface          │
   │  cffi    │ playwright│                     │
   └──────────┴───────────┘                     │
              │                                 │
              └────────────┬────────────────────┘
                           ▼
              ┌─────────────────────────┐
              │    ingest pipeline      │
              │  dedupe → parse → store │
              │       → write           │
              └────────────┬────────────┘
                           │  single writer, short transactions
                           ▼
              ┌─────────────────────────┐
              │        DuckDB           │
              │  files · activities     │
              │  laps  · records        │
              └────────────┬────────────┘
                           │  read-only, one connection per query
                           ▼
              ┌─────────────────────────┐
              │      MCP server         │
              │   5 typed tools, no     │
              │   generic SQL           │
              └────────────┬────────────┘
                           │  stdio  (or streamable HTTP)
                           ▼
                        Claude
```

Raw FIT files are kept forever under `data/raw/`. About 150 kB per activity —
a decade of triathlon fits in well under a gigabyte — which means the entire
history can be re-parsed whenever the parser learns a new field, with no
network involved.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv run garmin-mcp setup
```

That asks two questions, writes an owner-readable `.env`, creates the database,
logs in to Garmin, pulls your recent activities and prints the command to
connect it to Claude. Choose *manual* if you would rather not hand it your
Garmin password — the import path needs no account at all.

Then register the server:

```bash
claude mcp add garmin -- uv run --directory "$(pwd)" garmin-mcp serve
```

Restart Claude Code and ask it something.

<details>
<summary>Manual setup instead of the wizard</summary>

```bash
cp .env.example .env        # fill in GARMIN_EMAIL and GARMIN_PASSWORD
uv sync
uv run garmin-mcp init-db
uv run garmin-mcp auth      # interactive, handles MFA
uv run garmin-mcp sync --limit 50
```
</details>

## Commands

| Command | Purpose |
|---|---|
| `setup` | Interactive first run — credentials, database, login, verification |
| `auth` | Log in to Garmin and save the session (the only command that sees a password) |
| `sync` | Download and ingest new activities, incrementally |
| `import` | Ingest FIT files from `data/inbox/` — no network, no credentials |
| `serve` | Run the MCP server |
| `worker` | Background sync loop — the only process that writes |
| `status` | Report whether Garmin is currently reachable |
| `info` | Show what the database holds |

## Tools exposed to Claude

| Tool | Returns | Bound |
|---|---|---|
| `list_activities` | Compact record per activity | 20 by default, 200 max |
| `get_activity_detail` | Summary, laps, multisport legs | 50 laps |
| `get_activity_streams` | Columnar time series + true peaks | 200 points, 2000 max |
| `weekly_summary` | Per-sport totals for one week | one week |
| `compare_activities` | Two activities with deltas computed | fixed |
| `database_status` | What is stored, worker health, reachability | fixed |
| `sync_now` | Pull new activities without leaving the chat | needs the worker |

## Design decisions

The parts that were not obvious, and why they went the way they did.

### No generic SQL tool

Giving a language model arbitrary query access to a personal training database
is a liability rather than a feature. It can be talked into reading anything
the file holds, and it will occasionally write a query that scans a million
rows to answer a question about last Tuesday.

Every statement lives in [`db/queries.py`](src/garmin_mcp/db/queries.py), fully
parameterised. Stream field names go through an allow-list — they arrive from a
model, and that list is what keeps them out of the SQL text. One module to
audit, rather than a promise to trust.

### Manual import is a pillar, not a fallback

In March 2026 Garmin deployed Cloudflare TLS fingerprinting, which blocks
clients by the shape of their TLS handshake before authentication even begins.
`garth` — the library this project was originally specified to use — was
deprecated within days, and every plain HTTP client stopped working. It will
happen again.

So `data/inbox/` is a first-class ingestion path, tested as such. Drop FIT files
exported from Garmin Connect into it and run `garmin-mcp import`. No network, no
credentials, nothing that can be revoked. It is the only route that can honestly
be promised to still work in a year.

### Two backends behind one interface

[`ActivitySource`](src/garmin_mcp/garmin/source.py) is deliberately narrow —
list what exists, fetch one file, report health. Two implementations sit behind
it: a lightweight HTTP client impersonating Chrome's TLS handshake, and a real
headless Chromium for when that stops being enough. An official-API backend
drops in the day Garmin reopens its developer programme.

`auto` falls back to the browser only for failures a browser can actually fix.
An expired token is not one of them: no backend can invent a login you have not
performed, and falling back there would replace a clear *run `garmin-mcp auth`*
with a slow, confusing browser failure.

### Authentication cannot happen in the server

The backends are constructed without credentials, so they are structurally
incapable of starting a fresh login — they can only resume a saved token. That
is what lets the unattended ingest path fail loudly instead of hanging on an MFA
prompt nobody will answer, and it is why a dead Garmin session degrades into
"the history stops at last Tuesday" rather than a server that will not start.

Passwords are never written to disk by this project, never logged, and never
stored in the database. Only the OAuth token is persisted, `chmod 600`. Once
you have run `auth`, you can delete `GARMIN_PASSWORD` from `.env` entirely.

### A triathlon is one activity *and* six

A FIT file is a message stream, not "an activity". A normal run holds one
`session` message; a multisport recording holds several — swim, T1, bike, T2,
run — with transitions being real sessions of their own.

Stored as a parent row plus one leg per discipline. Lists show the parent, so a
triathlon reads as one line. Filtering by sport reveals the legs, so *my running
volume this month* correctly includes the 10 km inside a triathlon. Weekly
totals count top-level rows only, so 51.5 km is counted once rather than once
per leg.

More than one session does **not** imply multisport, incidentally: a file can
chain independent recordings, or repeat one twice. That is decided from the
`activity` message, with temporal contiguity and transition legs as fallback.

### Output is a budget

Every byte a tool returns is spent from a context window. Nulls are dropped;
units are resolved (`"4:42/km"` costs less than `avg_speed_mps: 3.5432` plus the
arithmetic to read it); runners get pace and cyclists get km/h but never both;
and series come back columnar rather than as objects, for roughly a third of the
tokens.

Streams are averaged into buckets — a three-hour ride holds ~11 000 samples per
channel. Because averaging flattens extremes, every stream response also carries
`true_range`: minimum, maximum and mean computed over every raw sample. Without
it, a coarse 10-point overview of a real ride reports a maximum heart rate of
160 against an actual 174, and states it with complete confidence.

## Device compatibility

The parser targets the FIT protocol, not one watch. It is validated against a
corpus of 42 real recordings spanning 19 devices from 8 manufacturers — Garmin
(fr70 through fēnix 5, Edge 200/500/800/810/820, fr920xt, vívoactive), Wahoo
ELEMNT and BOLT, Coros Pace 2, Stryd, Zwift, SigmaSport and the Strava mobile
app.

30 of the 42 parse. The other 12 are correct rejections: 11 are not activity
files (settings, workouts, weight scales, daily monitoring) and one is truncated
before its first session survived.

Quirks that only real hardware reveals, all handled:

- devices that log for 45 minutes before you press start (a fēnix 2 does),
  which would otherwise produce negative elapsed times;
- writers that record heart rate `0` instead of the "missing" sentinel,
  dragging every average down — while `0` cadence and `0` power are real
  readings from a coasting cyclist and are left alone;
- firmware writing `start_time` as an unresolvable integer, reconstructed from
  the next best anchor and flagged as such;
- files with no `activity` message at all, where the timezone would silently
  become UTC and file a Sunday evening run under Monday;
- cadence, which FIT stores in three incompatible units depending on sport.

Reconstructed values carry a provenance marker, so an inferred number is never
mistaken for a measured one.

```bash
make test-all      # fetches the corpus, then runs the deep suite
```

## Data model

| Table | Contents |
|---|---|
| `files` | One row per ingested FIT, keyed by content hash |
| `activities` | One row per session, plus a parent row for multisport |
| `laps` | Intervals — what makes a structured session legible |
| `records` | One sample per second: HR, pace, altitude, power, running dynamics |

Wide tables rather than key/value: DuckDB is columnar, so unused columns cost
almost nothing and `SELECT heart_rate` reads exactly one column. An `extra`
JSON column absorbs rare fields, so a new device never silently loses data.

Ingestion is idempotent. Identity is the content hash, so the same ride pulled
from Garmin and later dropped into the inbox by hand is recognised as one file
whatever it is named. Re-ingesting replaces rather than merges, inside a single
transaction.

## Testing

```bash
make test        # 133 tests, hermetic — no data, no network
make test-all    # 177 tests, adds validation against real recordings
```

The committed suite is entirely synthetic. `fitdecode` only reads FIT files, so
testing the parser would normally mean committing real recordings — but a GPS
trace starts at someone's front door, and that has no place in a public
repository. [`tests/fit_builder.py`](tests/fit_builder.py) is a minimal FIT
*encoder* written for the purpose: the suite runs anywhere after a clone, and it
can fabricate a multisport triathlon that the author never actually records.

The real-device corpus is third-party licensed and gitignored. Every quirk it
revealed is reproduced synthetically, so regressions are caught without it.

## Docker

```bash
docker compose up -d ingest          # background sync, the only writer
docker compose run --rm auth         # log in once (interactive)
docker compose logs -f ingest
```

One writer, enforced by the compose file: DuckDB grants exclusive access to a
single writer and blocks readers while it is held, so only `ingest` may write.
It stops on SIGTERM with a 30-second grace period rather than being killed
mid-transaction.

The image runs as a non-root user. `/data` is the only mutable path and the
only one worth persisting; the build context excludes it entirely, so no
database, FIT file or token can end up in a layer.

For the stdio transport the MCP client owns the process lifecycle, so register
the command with the client rather than starting it with compose:

```bash
claude mcp add garmin -- docker compose -f /abs/path/docker-compose.yml \
  run --rm -T mcp-stdio
```

`-T` matters: without it compose allocates a TTY and corrupts the JSON-RPC
stream on stdout.

| Profile | What it adds |
|---|---|
| *(default)* | `ingest` — the sync worker |
| `tools` | `auth`, `import` — one-shot commands |
| `http` | `mcp-http` — the server over streamable HTTP, bound to localhost |
| `playwright` | `ingest-playwright` — Chromium fallback, ~1 GB |

## Continuous integration

GitHub Actions runs, on every push and pull request: ruff (lint and format),
mypy in strict mode, pytest on Python 3.12 and 3.13, a Docker build with a
smoke test that the image actually starts, the corpus suite as a job of its
own, and a scan of **every commit in history** for credentials, databases and
FIT files.

That last job exists because this repository is built around personal data: a
secret committed by accident stays recoverable long after it is deleted from
the working tree, so checking the current state is not enough.

## Configuration

Two values matter, and only if you want automatic sync:

```
GARMIN_EMAIL=
GARMIN_PASSWORD=
```

Everything else in [`.env.example`](.env.example) already has a working default.

## Privacy

This repository is built on the assumption that training data is personal. GPS
traces start where you live.

- `.gitignore` was in the first commit, before any data existed: `.env`, tokens,
  `data/`, `*.duckdb`, `*.fit`.
- Nothing is sent anywhere. The database, the raw files and the tokens all stay
  on your machine.
- The MCP server has no authentication of its own. Under stdio that is fine —
  only the client that launched it can talk to it. If you switch to streamable
  HTTP, keep it bound to localhost.

## Limitations

- **The `cffi` backend is an arms race.** It works today. Garmin can change
  its fingerprinting at any time, and that is what the manual inbox is for.
- **The Playwright backend is unverified against a live account.** Its
  structure, error mapping and interface conformance are tested; its network
  calls are not. Expect to adjust the endpoint paths on first run.
- **Activities only.** HRV, sleep, Body Battery and training status are not
  ingested. The schema leaves room for them.
- **One user per database.** Multi-tenancy would be one database file per user
  rather than a `user_id` column.

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with or endorsed by Garmin. "Garmin" and "Garmin Connect" are
trademarks of Garmin Ltd.

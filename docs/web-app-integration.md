# Connecting a web application to this MCP server

What is true today, what is not, and what a multi-user product would have to
change. This is a list of limits, not a plan: building a multi-user platform is
out of scope.

## What a web app can rely on

- **A stable tool surface** with bounded outputs: `find_local_activities`,
  `get_activity_route`, `get_capabilities`, plus the existing tools.
- **Machine-readable errors and status**: stable `code`s and a `recovery` action
  (`capabilities.md`).
- **GPX over the protocol**, as MCP resources, so a remote client never needs a
  path on the server's disk (`route-tools.md`).
- **Local reads independent of Garmin**: an expired Garmin session degrades
  freshness, not availability.

## Limits that matter before any web exposure

1. **No authentication.** The streamable-HTTP transport has none. Whoever can
   reach the port reads the whole training history, GPS traces included — which
   start at the athlete's home. The server binds `127.0.0.1` by default and warns
   when told otherwise; `docker-compose.yml` publishes only `127.0.0.1:8000`. Do not
   put it on the Internet. Authentication belongs in front of it (a gateway that
   authenticates the caller and forwards only authenticated requests), not in the
   tool arguments.
2. **Writes share that exposure.** With `GARMIN_ENABLE_WRITES=true`, an
   unauthenticated caller can create workouts and overwrite activity notes on the
   athlete's Garmin account. The two-step `confirm` is a guard against a model
   acting on one call, not an access control. Keep writes off on anything reachable
   beyond the machine.
3. **Single user by construction.** One DuckDB file, one Garmin session directory,
   one worker, one `data/` tree. No tool takes a user id, and none should: identity
   must come from the authenticated transport, never from an argument a model
   supplies.
4. **DuckDB allows one writer and no concurrent readers.** The worker opens the
   file read-write briefly; the server retries reads for ~2 s and then reports
   `database_locked`. Fine for one athlete; a product with many simultaneous users
   needs a different store or one file per user.
5. **The worker and the server coordinate through files** under `data/.triggers/`
   and `data/.state/`. They must share a filesystem (the Docker volume does). This
   does not scale across hosts; a queue would replace it.
6. **Garmin access is per person and unofficial.** Credentials are entered through
   an interactive CLI (`garmin-mcp auth`, MFA included); there is no consent flow a
   web app can drive. The read path rests on `garminconnect` and on an undocumented
   `course-service` endpoint, behind Cloudflare client fingerprinting that has
   already broken one library. A multi-user product built on this would be fragile
   and may conflict with Garmin's terms; the sustainable route is Garmin's official
   developer programme, which `ActivitySource` is shaped to accept.
7. **A browser cannot call this server directly.** No CORS configuration, no
   authentication. A web app talks to it from its own backend, as an MCP client.
8. **Resources have no pagination or streaming.** A GPX is returned whole (3–4 MB
   for a 56 km race at 1 Hz). Fine for one route at a time; do not enumerate.
9. **Search covers the athlete's own history only.** `find_local_activities`
   answers "where have *I* run near here". It is not a national route search, and a
   user with no history gets an empty answer. A catalogue of public routes is
   explicitly not part of this server.
10. **No quota, no rate limit, no audit log of reads.** Logs carry counts and ids,
    not coordinates, but nothing records who asked for what.

## If this becomes multi-user

In order of what blocks first:

1. authenticate at a gateway and derive a tenant from the identity;
2. one data directory (database, `.state`, `.tokens`, inbox) per tenant, resolved
   from that identity before any tool runs, with a worker per tenant or a worker
   that serves one tenant per request;
3. replace the file-based trigger exchange with a queue;
4. per-tenant token storage encrypted at rest, and a consent/login flow that is not
   a terminal;
5. decide whether DuckDB-per-tenant is acceptable or the store moves;
6. a retention and deletion path for GPS data (it is personal data under GDPR).

None of these is started here.

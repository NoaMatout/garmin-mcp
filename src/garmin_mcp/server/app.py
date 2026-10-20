"""The MCP server.

Thin by design: it registers the functions in `tools.py` and does nothing else.
Keeping the tool bodies out of here is what lets them be tested as ordinary
Python, with no protocol in the way — and it means switching transport, or one
day swapping the SDK, touches this file alone.

The server is strictly read-only. It never authenticates to Garmin and never
writes to the database, which is why a broken Garmin session degrades into
"the history stops at last Tuesday" rather than a server that will not start.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from mcp.server import MCPServer

from garmin_mcp.domain.gpx import MIME_TYPE as GPX_MIME_TYPE
from garmin_mcp.logging import get_logger
from garmin_mcp.server import capabilities, route_tools, tools

log = get_logger(__name__)

INSTRUCTIONS = """\
Query a personal Garmin Connect training history: runs, rides, swims and
triathlons recorded on a sports watch, stored locally.

Start with list_activities to find an activity and its id, then
get_activity_detail for laps and structure, or get_activity_streams for the
second-by-second series.

Two things worth knowing about the data:

* A multisport event (a triathlon) is stored as one parent activity plus one
  leg per discipline, transitions included. Lists show the parent; filtering by
  sport shows the legs, so running volume correctly includes the run inside a
  triathlon.
* Distances are in metres and speeds in metres per second in the database, but
  tools return kilometres, pace and km/h already formatted.
* sleep_history reads nightly sleep to line up against training — use it
  together with list_activities or weekly_summary when a question is about
  recovery or load, not sport data alone. It only holds a rolling recent
  window (see database_status for how far back it goes), not full history.
* strength_history reads sets performed in the gym — reps and load per set,
  from the watch. It records no measure of effort: how close a set was to
  failure has to come from the athlete, and reps counted from the wrist are
  often wrong by one.
* list_courses and get_course_profile read saved routes (Garmin's "courses"),
  not activities — a course is a plan with a distance and elevation profile
  but nothing executed. Use these for pacing or nutrition planning around a
  future session; an activity later run from a course is a separate,
  unlinked record.

* find_local_activities and get_activity_route answer "where could I train for a
  race around here": recorded runs that started near a point, and their GPS
  track (simplified for a map, full resolution as a GPX resource). The radius
  is a straight line from the search point to each run's START point, not a
  travel time. A recorded run is not a validated or public route.
* get_capabilities says what this installation can do and what has actually
  been verified (and when), without contacting Garmin. Local reads keep working
  when the Garmin session has expired.

If a query returns nothing, call database_status before concluding an activity
does not exist — the database may simply be empty or still syncing.
"""


def _version() -> str:
    """The installed package version, reported during the MCP handshake.

    Clients show this, and a blank version makes a server look unfinished.
    """
    try:
        return version("garmin-mcp")
    except PackageNotFoundError:  # running from a source tree, not installed
        return "0.0.0+dev"


def create_server() -> MCPServer:
    """Build the server and register the tools."""
    mcp = MCPServer("garmin-mcp", version=_version(), instructions=INSTRUCTIONS)

    # Registered explicitly rather than by scanning the module: the set of
    # tools exposed to a model should be a deliberate list, not whatever
    # happens to be importable.
    for function in (
        tools.list_activities,
        tools.get_activity_detail,
        tools.get_activity_streams,
        tools.weekly_summary,
        tools.sleep_history,
        tools.list_courses,
        tools.get_course_profile,
        tools.plan_nutrition,
        tools.strength_history,
        route_tools.find_local_activities,
        route_tools.get_activity_route,
        capabilities.get_capabilities,
        tools.compare_activities,
        tools.compare_to_plan,
        tools.plan_adherence,
        tools.database_status,
        tools.sync_now,
        tools.list_workouts,
        tools.create_workout,
        tools.delete_workout,
        tools.schedule_workout,
        tools.list_scheduled_workouts,
        tools.unschedule_workout,
        tools.set_activity_notes,
    ):
        mcp.tool()(function)

    # GPX is served as MCP resources rather than written to disk: a resource is
    # read over the same connection as everything else, so it works the same
    # over stdio and over HTTP, and no client has to share a filesystem with the
    # server. Generated on demand, so there is nothing to expire or clean up.
    mcp.resource(
        route_tools.ACTIVITY_GPX_URI,
        name="activity_route_gpx",
        title="Recorded activity track (GPX)",
        description="Every valid GPS fix of a recorded activity, unsimplified, as GPX 1.1.",
        mime_type=GPX_MIME_TYPE,
    )(route_tools.activity_route_gpx)
    mcp.resource(
        route_tools.COURSE_GPX_URI,
        name="course_route_gpx",
        title="Saved course (GPX)",
        description="A saved Garmin course's stored geometry as GPX 1.1.",
        mime_type=GPX_MIME_TYPE,
    )(route_tools.course_route_gpx)

    log.info("server.created", tools=len(tool_names()))
    return mcp


def tool_names() -> list[str]:
    """Names of the registered tools, for tests and diagnostics."""
    return [
        "list_activities",
        "get_activity_detail",
        "get_activity_streams",
        "weekly_summary",
        "sleep_history",
        "list_courses",
        "get_course_profile",
        "plan_nutrition",
        "strength_history",
        "compare_activities",
        "compare_to_plan",
        "find_local_activities",
        "get_activity_route",
        "get_capabilities",
        "plan_adherence",
        "database_status",
        "sync_now",
        "list_workouts",
        "create_workout",
        "delete_workout",
        "schedule_workout",
        "list_scheduled_workouts",
        "unschedule_workout",
        "set_activity_notes",
    ]


# Deliberately no generic `run(transport, **kwargs)` wrapper here. SDK v2
# overloads `run()` per transport so a type checker validates which keywords
# each one accepts; funnelling everything through `**kwargs` would throw that
# away and let `port=` on stdio through silently. transports.py calls the
# typed methods directly instead.

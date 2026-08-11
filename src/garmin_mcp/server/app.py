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
from typing import Any

from mcp.server import MCPServer

from garmin_mcp.logging import get_logger
from garmin_mcp.server import tools

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
        tools.compare_activities,
        tools.database_status,
    ):
        mcp.tool()(function)

    log.info("server.created", tools=6)
    return mcp


def tool_names() -> list[str]:
    """Names of the registered tools, for tests and diagnostics."""
    return [
        "list_activities",
        "get_activity_detail",
        "get_activity_streams",
        "weekly_summary",
        "compare_activities",
        "database_status",
    ]


def run(transport: str = "stdio", **kwargs: Any) -> None:
    """Start the server on the given transport.

    In SDK v2 the transport is an argument to `run()`, so stdio and streamable
    HTTP differ by a parameter rather than by a rewrite.
    """
    create_server().run(transport=transport, **kwargs)  # type: ignore[arg-type]

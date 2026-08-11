"""Transport selection.

Small on purpose. The requirement was "stdio first, structured so HTTP is a
switch rather than a rewrite", and SDK v2 already delivers that by taking the
transport as an argument to `run()`. What is left worth writing down is the
one operational trap that separates the two modes.

**Under stdio, stdout belongs to the protocol.** The MCP client parses JSON-RPC
frames from this process's standard output. A stray `print`, a library writing
a warning there, or a logger left on its default stream corrupts the stream and
the client drops the connection with an opaque parse error. `garmin_mcp.logging`
pins every logger to stderr, and the check below fails loudly at startup if
something has redirected stdout anyway — a clear error beats a mystery.
"""

from __future__ import annotations

import sys

from garmin_mcp.config import Settings, Transport, get_settings
from garmin_mcp.logging import get_logger
from garmin_mcp.server.app import run as run_server

log = get_logger(__name__)


def _assert_stdout_is_clean() -> None:
    """Refuse to start if anything has taken over stdout.

    Cheap insurance against the single most confusing failure mode this server
    has: a client that connects, reads one malformed frame and disconnects.
    """
    if sys.stdout is not sys.__stdout__:
        raise RuntimeError(
            "stdout has been redirected, which corrupts the JSON-RPC stream "
            "under the stdio transport. Log to stderr instead."
        )


def run_stdio() -> None:
    """Serve over stdio — the mode a desktop MCP client launches."""
    _assert_stdout_is_clean()
    log.info("server.starting", transport="stdio")
    run_server(transport="stdio")


def run_http(host: str, port: int, *, stateless: bool = False) -> None:
    """Serve over streamable HTTP — for a container or a remote client.

    Binds to 127.0.0.1 unless told otherwise. This server exposes an entire
    training history, including GPS traces that start at the athlete's home,
    and has no authentication of its own; putting it on 0.0.0.0 publishes that
    to the network.
    """
    log.info("server.starting", transport="streamable-http", host=host, port=port)
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "server.listening_beyond_localhost",
            host=host,
            detail="this server has no authentication; anyone who can reach "
            "this address can read the full training history",
        )
    run_server(transport="streamable-http", host=host, port=port, stateless_http=stateless)


def serve(settings: Settings | None = None) -> None:
    """Start the server on the transport configured in the environment."""
    settings = settings or get_settings()
    if settings.mcp_transport is Transport.HTTP:
        run_http(settings.mcp_host, settings.mcp_port)
    else:
        run_stdio()

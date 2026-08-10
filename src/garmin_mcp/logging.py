"""Structured logging.

The single most important rule in this file: **everything goes to stderr**.

Under the stdio transport, stdout carries the JSON-RPC frames that the MCP
client parses. One stray `print()` or a logger defaulting to stdout corrupts
the stream and the client drops the connection with an opaque parse error.
Configuring both structlog and the stdlib logging root to stderr is what makes
that failure impossible rather than merely unlikely.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from garmin_mcp.config import LogFormat, Settings, get_settings

_configured = False


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Wire up structlog + stdlib logging. Idempotent unless `force`."""
    global _configured
    if _configured and not force:
        return

    settings = settings or get_settings()
    level = getattr(logging, settings.log_level)

    # stdlib root logger → stderr. Third-party libraries (duckdb, urllib3,
    # garminconnect) log through this, so it has to be redirected too.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
        force=True,
    )

    # These are chatty and say nothing we need at INFO.
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.log_format is LogFormat.JSON:
        processors += [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors += [
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # PrintLoggerFactory defaults to stdout — pinning stderr is the whole
        # point of this module.
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring logging on first use."""
    configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]

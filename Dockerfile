# syntax=docker/dockerfile:1
# check=skip=SecretsUsedInArgOrEnv
#
# The skip above is for GARMIN_TOKEN_DIR: the linter matches on the word
# "TOKEN", but the value is a directory path. No secret is baked into this
# image — the token is written at runtime into the mounted volume.
#
# Two targets:
#   runtime     (default) — the HTTP backend only. Small.
#   playwright            — adds Chromium for the browser fallback. ~1 GB.
#
#   docker build -t garmin-mcp .
#   docker build -t garmin-mcp:playwright --target playwright .
#
# Dependencies are installed in a layer of their own, before the source is
# copied, so editing code does not re-resolve the lockfile on every build.

ARG PYTHON_VERSION=3.12

# ─── builder ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Lockfile layer: unchanged dependencies mean this is reused.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# `--frozen` is what makes the image reproducible: it fails rather than
# silently resolving something newer than the committed lockfile.
COPY README.md LICENSE ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ─── runtime ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# Non-root. This container holds a Garmin token and a full GPS history;
# there is no reason for it to run as root.
RUN groupadd --gid 1000 garmin \
    && useradd --uid 1000 --gid garmin --create-home garmin

WORKDIR /app

COPY --from=builder --chown=garmin:garmin /app/.venv /app/.venv
COPY --from=builder --chown=garmin:garmin /app/src /app/src
COPY --chown=garmin:garmin pyproject.toml README.md LICENSE ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GARMIN_DATA_DIR=/data \
    GARMIN_DB_PATH=/data/garmin.duckdb \
    GARMIN_TOKEN_DIR=/data/.tokens

# Everything mutable lives here, and it is the only path worth persisting.
RUN mkdir -p /data && chown garmin:garmin /data
VOLUME ["/data"]

USER garmin

# Reports unhealthy once the worker stops writing its heartbeat, which is the
# thing that actually matters — the process staying alive is not the same as
# it still syncing.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["garmin-mcp", "status"]

ENTRYPOINT ["garmin-mcp"]
CMD ["worker"]

# ─── playwright ───────────────────────────────────────────────────────
# Only worth building if Garmin starts refusing the HTTP client. Kept as a
# separate target so the common case is not paying for a browser it will
# never launch.
FROM runtime AS playwright

USER root

RUN --mount=type=cache,target=/root/.cache/uv \
    /app/.venv/bin/python -m pip install --no-cache-dir "playwright>=1.47" \
    && /app/.venv/bin/playwright install --with-deps chromium \
    && chown -R garmin:garmin /root/.cache 2>/dev/null || true

# Playwright caches browsers under $HOME; the runtime user needs its own copy.
ENV PLAYWRIGHT_BROWSERS_PATH=/home/garmin/.cache/ms-playwright
RUN mkdir -p ${PLAYWRIGHT_BROWSERS_PATH} \
    && /app/.venv/bin/playwright install chromium \
    && chown -R garmin:garmin /home/garmin/.cache

USER garmin
ENV GARMIN_BACKEND=playwright

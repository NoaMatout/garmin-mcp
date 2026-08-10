"""Configuration, loaded from the environment and `.env`.

Every setting is prefixed `GARMIN_` and has a working default, so the project
runs with an empty `.env` as long as you only use manual import. Credentials
are the sole exception: they have no default and are only read by the auth
command.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Backend(StrEnum):
    CFFI = "cffi"
    PLAYWRIGHT = "playwright"
    AUTO = "auto"


class Transport(StrEnum):
    STDIO = "stdio"
    HTTP = "streamable-http"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GARMIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── Credentials ──────────────────────────────────────────────────
    email: str | None = None
    password: SecretStr | None = None

    # ─── Backend selection ────────────────────────────────────────────
    backend: Backend = Backend.AUTO
    token_dir: Path = Path("./data/.tokens")

    # ─── Storage ──────────────────────────────────────────────────────
    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/garmin.duckdb")

    # ─── Ingestion ────────────────────────────────────────────────────
    sync_interval_minutes: int = Field(default=30, ge=1)
    sync_batch_size: int = Field(default=25, ge=1, le=200)

    # ─── MCP server ───────────────────────────────────────────────────
    mcp_transport: Transport = Transport.STDIO
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8000, ge=1, le=65535)

    # ─── Logging ──────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        level = v.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return level

    # ─── Derived paths ────────────────────────────────────────────────
    # Kept as properties rather than fields so that overriding data_dir in a
    # test moves the whole tree in one go.

    @property
    def raw_dir(self) -> Path:
        """Downloaded FIT files, kept forever so we can always re-parse."""
        return self.data_dir / "raw"

    @property
    def inbox_dir(self) -> Path:
        """Manual-import drop folder — the degraded mode that always works."""
        return self.data_dir / "inbox"

    @property
    def inbox_processed_dir(self) -> Path:
        return self.inbox_dir / "_processed"

    @property
    def inbox_failed_dir(self) -> Path:
        return self.inbox_dir / "_failed"

    @property
    def trigger_dir(self) -> Path:
        """Where the MCP server drops sync requests for the worker."""
        return self.data_dir / ".triggers"

    def ensure_dirs(self) -> None:
        """Create the whole data tree. Idempotent, safe to call on every run."""
        for path in (
            self.data_dir,
            self.raw_dir,
            self.inbox_dir,
            self.inbox_processed_dir,
            self.inbox_failed_dir,
            self.trigger_dir,
            self.token_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def raw_path_for(self, activity_id: int, started_at_year: int, started_at_month: int) -> Path:
        """Stable on-disk location for a raw FIT file.

        Sharded by year/month so the directory stays browsable after a few
        thousand activities.
        """
        return self.raw_dir / f"{started_at_year:04d}" / f"{started_at_month:02d}" / f"{activity_id}.fit"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached because config is read on every DuckDB connection; call
    `get_settings.cache_clear()` in tests that need a different environment.
    """
    return Settings()

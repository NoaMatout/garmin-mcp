"""Shared fixtures.

Every test runs against a throwaway data directory and synthetic FIT files, so
the suite never touches the developer's real database or activities.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from garmin_mcp.config import Settings, get_settings
from tests import fit_builder


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    """Settings pointing at an isolated temporary data tree."""
    cfg = Settings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "garmin.duckdb",
        token_dir=tmp_path / "data" / ".tokens",
        email=None,
        password=None,
    )
    cfg.ensure_dirs()
    get_settings.cache_clear()
    yield cfg
    get_settings.cache_clear()


@pytest.fixture
def run_fit(tmp_path: Path) -> Path:
    """A single-session run: the common case."""
    path = tmp_path / "run.fit"
    path.write_bytes(fit_builder.build_run())
    return path


@pytest.fixture
def triathlon_fit(tmp_path: Path) -> Path:
    """A multisport file: swim, T1, bike, T2, run in one recording."""
    path = tmp_path / "triathlon.fit"
    path.write_bytes(fit_builder.build_triathlon())
    return path


@pytest.fixture
def corrupt_fit(tmp_path: Path) -> Path:
    """Valid header, truncated body — an interrupted download."""
    path = tmp_path / "corrupt.fit"
    path.write_bytes(fit_builder.build_corrupt())
    return path


@pytest.fixture
def non_activity_fit(tmp_path: Path) -> Path:
    """A well-formed FIT file that carries no session (settings export)."""
    path = tmp_path / "settings.fit"
    path.write_bytes(fit_builder.build_without_session())
    return path

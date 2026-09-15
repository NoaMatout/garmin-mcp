"""Tests for reading a gym session back out of a FIT file.

A strength file has almost nothing a run does — no records, no distance, no
GPS — so most of what could go wrong here is the pipeline assuming those
exist. The rest is arithmetic that is easy to get quietly wrong: Garmin
stores loads in sixteenths of a kilogram, and writes a `set` message for
rests as well as for working sets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from garmin_mcp.config import Settings
from garmin_mcp.db import queries
from garmin_mcp.db.migrations import PARSER_VERSION, init_database
from garmin_mcp.ingest.fit_parser import parse_fit
from garmin_mcp.ingest.writer import write_parsed
from garmin_mcp.server import tools
from tests.fit_builder import build_strength

SESSION = [
    {"reps": 3, "weight": 112.5, "category": "bench_press"},
    {"reps": 3, "weight": 105.0, "category": "bench_press"},
    {"reps": 3, "weight": 105.0, "category": "bench_press"},
    {"reps": 3, "weight": 105.0, "category": "bench_press"},
    {"reps": 3, "weight": 50.0, "category": "pull_up"},
    {"reps": 2, "weight": 45.0, "category": "pull_up"},
]


@pytest.fixture
def db(settings: Settings) -> Settings:
    init_database(settings)
    return settings


def _ingest(settings: Settings, parsed: Any) -> Any:
    """Write a parsed file, holding the single writer lock only briefly."""
    with duckdb.connect(str(settings.db_path)) as conn:
        return write_parsed(conn, parsed, parser_version=PARSER_VERSION)


def _read(settings: Settings, fn: Any, *args: Any, **kwargs: Any) -> Any:
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        return fn(conn, *args, **kwargs)


def _parse(tmp_path: Path, **kwargs: Any) -> Any:
    path = tmp_path / "strength.fit"
    path.write_bytes(build_strength(**kwargs))
    return parse_fit(path)


class TestParsingSets:
    def test_working_sets_and_rests_are_both_kept(self, tmp_path: Path) -> None:
        # The gap between two sets is training data: an athlete who cut their
        # rests short did a different session from the one prescribed.
        activity = _parse(tmp_path, sets=SESSION).activities[0]
        assert len(activity.sets) == len(SESSION) * 2
        assert sum(1 for s in activity.sets if not s.is_rest) == len(SESSION)

    def test_the_load_survives_garmins_sixteenths_of_a_kilo(self, tmp_path: Path) -> None:
        """112.5 kg is stored as 1800. A missing scale turns it into a fiction."""
        activity = _parse(tmp_path, sets=SESSION).activities[0]
        working = [s for s in activity.sets if not s.is_rest]
        assert working[0].weight_kg == pytest.approx(112.5)
        assert working[4].weight_kg == pytest.approx(50.0)

    def test_reps_and_movement_come_through(self, tmp_path: Path) -> None:
        working = [s for s in _parse(tmp_path, sets=SESSION).activities[0].sets if not s.is_rest]
        assert [s.repetitions for s in working] == [3, 3, 3, 3, 3, 2]
        assert working[0].category == "bench_press"
        assert working[4].category == "pull_up"

    def test_rests_carry_their_duration_and_nothing_else(self, tmp_path: Path) -> None:
        rest = next(s for s in _parse(tmp_path, sets=SESSION).activities[0].sets if s.is_rest)
        assert rest.duration_s == pytest.approx(210)
        assert rest.repetitions is None
        assert rest.weight_kg is None

    def test_a_file_with_no_records_still_parses(self, tmp_path: Path) -> None:
        """A gym session has no track. Nothing downstream may assume one."""
        parsed = _parse(tmp_path, sets=SESSION)
        activity = parsed.activities[0]
        assert activity.records == []
        assert activity.sub_sport == "strength_training"
        assert parsed.num_sessions == 1


class TestWritingSets:
    def test_sets_reach_the_database(self, db: Settings, tmp_path: Path) -> None:
        parsed = _parse(tmp_path, sets=SESSION)
        result = _ingest(db, parsed)

        assert result.sets_written == len(SESSION) * 2
        rows = _read(db, queries.strength_sets, parsed.activities[0].activity_id)
        assert len(rows) == len(SESSION) * 2
        assert rows[0]["weight_kg"] == pytest.approx(112.5)

    def test_reingesting_the_same_file_does_not_duplicate_sets(
        self, db: Settings, tmp_path: Path
    ) -> None:
        parsed = _parse(tmp_path, sets=SESSION)
        _ingest(db, parsed)
        _ingest(db, parsed)

        rows = _read(db, queries.strength_sets, parsed.activities[0].activity_id)
        assert len(rows) == len(SESSION) * 2


class TestStrengthHistory:
    def test_identical_sets_collapse_into_one_block(self, db: Settings, tmp_path: Path) -> None:
        """Three back-off sets at the same load are one line, not three.

        That is how the session was prescribed, and how an athlete reads it
        back.
        """
        _ingest(db, _parse(tmp_path, sets=SESSION))
        rows = _read(db, queries.strength_history)

        bench = [r for r in rows if r["category"] == "bench_press"]
        backoff = next(r for r in bench if r["weight_kg"] == pytest.approx(105.0))
        assert backoff["sets"] == 3
        assert backoff["total_reps"] == 9

    def test_rests_are_not_counted_as_work(self, db: Settings, tmp_path: Path) -> None:
        _ingest(db, _parse(tmp_path, sets=SESSION))
        rows = _read(db, queries.strength_history)
        assert all(r["category"] is not None for r in rows)

    def test_filtering_by_movement_is_loose(self, db: Settings, tmp_path: Path) -> None:
        _ingest(db, _parse(tmp_path, sets=SESSION))
        rows = _read(db, queries.strength_history, exercise="bench")
        assert rows
        assert all(r["category"] == "bench_press" for r in rows)


class TestStrengthTool:
    def test_a_block_reads_the_way_a_session_is_written(
        self, db: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ingest(db, _parse(tmp_path, sets=SESSION))
        monkeypatch.setattr(tools, "_settings", lambda: db)

        blocks = tools.strength_history()["sessions"]
        assert any(b["block"] == "3 × 3 @ 105 kg" for b in blocks)

    def test_a_short_set_shows_the_rep_range(
        self, db: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sets that were not all completed are the interesting case."""
        sets = [
            {"reps": 3, "weight": 50.0, "category": "pull_up"},
            {"reps": 2, "weight": 50.0, "category": "pull_up"},
        ]
        _ingest(db, _parse(tmp_path, sets=sets))
        monkeypatch.setattr(tools, "_settings", lambda: db)

        block = tools.strength_history(exercise="pull_up")["sessions"][0]
        assert block["block"] == "2 × 2-3 @ 50 kg"

    def test_one_session_can_be_read_in_full_with_its_rests(
        self, db: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed = _parse(tmp_path, sets=SESSION)
        _ingest(db, parsed)
        monkeypatch.setattr(tools, "_settings", lambda: db)

        result = tools.strength_history(activity_id=parsed.activities[0].activity_id)
        assert result["count"] == len(SESSION) * 2
        assert any("rest" in s for s in result["sets"])

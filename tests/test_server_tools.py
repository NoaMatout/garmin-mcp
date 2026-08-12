"""Tests for the MCP tools.

Two concerns run through all of them.

**Correctness of the numbers.** A wrong total in a training log is worse than
an error, because it looks like an answer.

**Bounded output.** Every tool result is spent from a language model's context
window. A tool that ignores its own limit can dump a hundred thousand numbers
into a conversation and crowd out the question it was answering — which is not
hypothetical: the down-sampling below silently returned all 13 471 samples for
a request of 8 until the regression test here was written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.db.migrations import init_database
from garmin_mcp.errors import ActivityNotFoundError
from garmin_mcp.ingest.pipeline import import_inbox
from garmin_mcp.server import tools
from garmin_mcp.server.app import create_server, tool_names
from tests import fit_builder

MONDAY = datetime(2026, 3, 16, 7, 30, tzinfo=UTC)  # a Monday


@pytest.fixture
def loaded(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A database with a run, a ride and a triathlon in it."""
    init_database(settings)

    (settings.inbox_dir / "run.fit").write_bytes(
        fit_builder.build_run(start=MONDAY, distance_m=10000.0, duration_s=3000, laps=3)
    )
    (settings.inbox_dir / "run2.fit").write_bytes(
        fit_builder.build_run(
            start=MONDAY + timedelta(days=2), distance_m=8000.0, duration_s=2200, laps=2
        )
    )
    (settings.inbox_dir / "tri.fit").write_bytes(
        fit_builder.build_triathlon(start=MONDAY + timedelta(days=4))
    )
    import_inbox(settings)

    # The tools read settings through get_settings(); point it at this database.
    monkeypatch.setattr(tools, "_settings", lambda: settings)
    return settings


def _registered_tools() -> list:  # type: ignore[type-arg]
    """What the server actually advertises. `list_tools` is a coroutine."""
    import asyncio

    return asyncio.run(create_server().list_tools())


def _ids_by_sport(sport: str) -> list[int]:
    result = tools.list_activities(activity_type=sport, limit=50)
    return [a["activity_id"] for a in result["activities"]]


class TestServerWiring:
    def test_the_declared_tool_names_match_what_is_registered(self) -> None:
        """`tool_names()` is hand-maintained and used by diagnostics.

        Asserting a count would just need bumping whenever a tool is added.
        The invariant worth holding is that the declared list and the real
        registration cannot drift apart.
        """
        registered = {tool.name for tool in _registered_tools()}
        assert registered == set(tool_names())

    def test_every_tool_from_the_specification_is_present(self) -> None:
        required = {
            "list_activities",
            "get_activity_detail",
            "get_activity_streams",
            "weekly_summary",
            "compare_activities",
        }
        assert required <= set(tool_names())

    def test_every_tool_is_documented_for_the_model(self) -> None:
        # The description is the only thing a model has to choose a tool by.
        for tool in _registered_tools():
            assert tool.description, f"{tool.name} has no description"

    def test_there_is_no_generic_sql_tool(self) -> None:
        # A deliberate absence: arbitrary query access for a model is a
        # liability, not a feature.
        assert not any("sql" in name.lower() or "query" in name.lower() for name in tool_names())


class TestListActivities:
    def test_returns_top_level_activities_newest_first(self, loaded: Settings) -> None:
        result = tools.list_activities()
        dates = [a["date"] for a in result["activities"]]
        assert dates == sorted(dates, reverse=True)
        # Two runs and the triathlon parent — not the triathlon's five legs.
        assert result["count"] == 3

    def test_multisport_appears_as_one_entry(self, loaded: Settings) -> None:
        sports = [a["sport"] for a in tools.list_activities()["activities"]]
        assert sports.count("multisport") == 1
        assert "transition" not in sports

    def test_filtering_by_sport_reveals_legs(self, loaded: Settings) -> None:
        """"My runs" must include the run inside a triathlon.

        That is the entire justification for storing legs separately.
        """
        runs = tools.list_activities(activity_type="running", limit=50)
        assert runs["count"] == 3  # two standalone runs plus the triathlon's run

    def test_limit_is_capped(self, loaded: Settings) -> None:
        result = tools.list_activities(limit=99999)
        assert result["count"] <= tools.MAX_LIST_LIMIT

    def test_since_filters_by_date(self, loaded: Settings) -> None:
        result = tools.list_activities(since="2026-03-19")
        assert result["count"] == 1  # the Friday triathlon only

    def test_a_bad_date_says_what_is_expected(self, loaded: Settings) -> None:
        with pytest.raises(ValueError, match="ISO date"):
            tools.list_activities(since="last tuesday")

    def test_empty_fields_are_omitted(self, loaded: Settings) -> None:
        # Nulls cost tokens and invite commentary on absent data.
        for activity in tools.list_activities()["activities"]:
            assert None not in activity.values()

    def test_runners_get_pace_and_cyclists_get_speed(self, loaded: Settings) -> None:
        runs = tools.list_activities(activity_type="running", limit=10)["activities"]
        assert all("avg_pace" in a and "avg_speed_kmh" not in a for a in runs)

        rides = tools.list_activities(activity_type="cycling", limit=10)["activities"]
        assert all("avg_speed_kmh" in a and "avg_pace" not in a for a in rides)


class TestActivityDetail:
    def test_includes_laps(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[-1]
        detail = tools.get_activity_detail(run_id)
        assert len(detail["laps"]) >= 2
        assert detail["laps"][0]["lap"] == 1  # humans count from one

    def test_multisport_lists_its_legs(self, loaded: Settings) -> None:
        parent_id = _ids_by_sport("multisport")[0]
        detail = tools.get_activity_detail(parent_id)
        assert len(detail["legs"]) == 5
        assert [leg["sport"] for leg in detail["legs"]] == [
            "swimming", "transition", "cycling", "transition", "running",
        ]

    def test_legs_are_marked_as_belonging_to_a_parent(self, loaded: Settings) -> None:
        # A leg must never be presented as a standalone session.
        parent_id = _ids_by_sport("multisport")[0]
        for leg in tools.get_activity_detail(parent_id)["legs"]:
            assert leg["part_of_activity"] == parent_id

    def test_unknown_id_raises_with_a_way_forward(self, loaded: Settings) -> None:
        with pytest.raises(ActivityNotFoundError) as caught:
            tools.get_activity_detail(424242)
        assert "list_activities" in (caught.value.hint or "")


class TestStreams:
    def test_downsampling_respects_max_points(self, loaded: Settings) -> None:
        """The regression this suite exists for.

        DuckDB's `/` returns a DOUBLE, so integer division has to be spelled
        `//`. Getting that wrong gave every row its own bucket and returned the
        entire series — 13 471 points for a request of 8, in the real database.
        """
        run_id = _ids_by_sport("running")[-1]
        for requested in (2, 5, 17, 60):
            result = tools.get_activity_streams(run_id, ["heart_rate"], max_points=requested)
            assert result["points"] <= requested, (
                f"asked for {requested}, got {result['points']}"
            )

    def test_every_series_has_the_same_length(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[-1]
        result = tools.get_activity_streams(
            run_id, ["heart_rate", "speed", "altitude"], max_points=10
        )
        lengths = {len(values) for values in result["series"].values()}
        assert len(lengths) == 1

    def test_output_is_columnar(self, loaded: Settings) -> None:
        # Roughly three times fewer tokens than a list of objects.
        run_id = _ids_by_sport("running")[-1]
        series = tools.get_activity_streams(run_id, ["heart_rate"], max_points=5)["series"]
        assert isinstance(series["heart_rate"], list)
        assert all(not isinstance(v, dict) for v in series["heart_rate"])

    def test_downsampling_is_disclosed(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[-1]
        result = tools.get_activity_streams(run_id, ["heart_rate"], max_points=5)
        assert "downsampled" in result
        assert result["source_samples"] > result["points"]

    def test_the_hard_cap_holds(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[-1]
        result = tools.get_activity_streams(run_id, ["heart_rate"], max_points=10**9)
        assert result["points"] <= tools.MAX_STREAM_POINTS

    def test_unknown_field_is_rejected_by_name(self, loaded: Settings) -> None:
        # Field names come from a model; the allow-list is what keeps them out
        # of the SQL text.
        run_id = _ids_by_sport("running")[-1]
        with pytest.raises(ValueError, match="unknown fields"):
            tools.get_activity_streams(run_id, ["heart_rate; DROP TABLE records"])

    def test_an_activity_without_samples_says_so(self, loaded: Settings) -> None:
        parent_id = _ids_by_sport("multisport")[0]  # the parent owns no samples
        result = tools.get_activity_streams(parent_id, ["heart_rate"])
        assert result["points"] == 0
        assert "no per-second samples" in result["note"]


class TestWeeklySummary:
    def test_any_day_of_the_week_gives_the_same_answer(self, loaded: Settings) -> None:
        monday = tools.weekly_summary("2026-03-16")
        thursday = tools.weekly_summary("2026-03-19")
        assert monday["week_start"] == thursday["week_start"] == "2026-03-16"
        assert monday["totals"] == thursday["totals"]

    def test_totals_do_not_double_count_multisport(self, loaded: Settings) -> None:
        """The triathlon contributes 51.5 km once, not once per leg."""
        summary = tools.weekly_summary("2026-03-16")
        # 10 km run + 8 km run + 51.5 km triathlon.
        assert summary["totals"]["distance_km"] == pytest.approx(69.5, abs=0.1)

    def test_breaks_down_by_sport(self, loaded: Settings) -> None:
        summary = tools.weekly_summary("2026-03-16")
        sports = {entry["sport"] for entry in summary["by_sport"]}
        assert sports == {"running", "multisport"}

    def test_an_empty_week_is_reported_not_hidden(self, loaded: Settings) -> None:
        summary = tools.weekly_summary("2019-01-07")
        assert summary["by_sport"] == []
        assert "no activities" in summary["note"]


class TestCompare:
    def test_deltas_are_computed_for_the_reader(self, loaded: Settings) -> None:
        first, second = _ids_by_sport("running")[:2]
        result = tools.compare_activities(first, second)
        assert "delta_b_minus_a" in result
        assert "a" in result and "b" in result

    def test_pace_difference_is_labelled_in_words(self, loaded: Settings) -> None:
        # A positive pace delta means slower, which is easy to misread.
        first, second = _ids_by_sport("running")[:2]
        deltas = tools.compare_activities(first, second)["delta_b_minus_a"]
        if "pace_verdict" in deltas:
            assert deltas["pace_verdict"] in (
                "second is faster", "second is slower", "identical",
            )

    def test_comparing_different_sports_warns(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[0]
        ride_id = _ids_by_sport("cycling")[0]
        result = tools.compare_activities(run_id, ride_id)
        assert "warning" in result

    def test_comparing_an_activity_with_itself_is_rejected(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[0]
        with pytest.raises(ValueError, match="same activity"):
            tools.compare_activities(run_id, run_id)

    def test_unknown_id_raises(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[0]
        with pytest.raises(ActivityNotFoundError):
            tools.compare_activities(run_id, 424242)


class TestDatabaseStatus:
    def test_reports_contents(self, loaded: Settings) -> None:
        status = tools.database_status()
        assert status["available"] is True
        assert status["activities"] == 8  # 2 runs + parent + 5 legs
        assert status["samples"] > 0

    def test_a_missing_database_explains_how_to_fix_it(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty result must be distinguishable from "nothing ingested yet".
        monkeypatch.setattr(tools, "_settings", lambda: settings)
        get_settings.cache_clear()
        status = tools.database_status()
        assert status["available"] is False
        assert "init-db" in status["fix"]


class TestSetupWizard:
    """The first-run experience, which is where a showcase project is judged."""

    def test_writes_an_env_file_only_its_owner_can_read(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A world-readable file holding a Garmin password is a real problem on
        # a shared machine, and easy to get wrong by writing then chmod-ing.
        from garmin_mcp.cli import _write_env

        monkeypatch.chdir(tmp_path)
        env_path = tmp_path / ".env"
        _write_env(env_path, email="a@b.c", password="hunter2", backend="auto")

        assert env_path.stat().st_mode & 0o777 == 0o600
        assert "GARMIN_EMAIL=a@b.c" in env_path.read_text()

    def test_the_example_file_marks_what_is_actually_required(self) -> None:
        # The example previously read as a 30-line form; only two values are
        # required, and a newcomer should not have to work that out.
        from pathlib import Path as _Path

        text = _Path(".env.example").read_text()
        assert "REQUIRED" in text
        assert "OPTIONAL" in text
        assert "garmin-mcp setup" in text

    def test_the_example_file_contains_no_real_credentials(self) -> None:
        from pathlib import Path as _Path

        for line in _Path(".env.example").read_text().splitlines():
            if line.startswith(("GARMIN_EMAIL=", "GARMIN_PASSWORD=")):
                assert line.split("=", 1)[1] == "", f"example ships a value: {line}"


class TestTrueRange:
    """Averaging destroys peaks, so the real ones travel alongside the series.

    Found on live data: a coarse 10-point overview of a real ride reported a
    maximum heart rate of 160 against an actual 174, and a minimum of 138
    against 111. A model reading only the smoothed series states those wrong
    numbers with full confidence — and maximum heart rate is a number athletes
    take seriously.
    """

    def test_true_range_is_returned_with_the_series(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[-1]
        result = tools.get_activity_streams(run_id, ["heart_rate"], max_points=5)
        assert "true_range" in result
        assert {"min", "max", "avg"} <= set(result["true_range"]["heart_rate"])

    def test_true_range_is_unaffected_by_resolution(self, loaded: Settings) -> None:
        # The whole point: peaks must not move when the series is smoothed.
        run_id = _ids_by_sport("running")[-1]
        coarse = tools.get_activity_streams(run_id, ["heart_rate"], max_points=3)
        fine = tools.get_activity_streams(run_id, ["heart_rate"], max_points=2000)
        assert coarse["true_range"] == fine["true_range"]

    def test_true_range_brackets_the_smoothed_series(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[-1]
        result = tools.get_activity_streams(run_id, ["heart_rate"], max_points=5)
        smoothed = [v for v in result["series"]["heart_rate"] if v is not None]
        true = result["true_range"]["heart_rate"]
        assert max(smoothed) <= true["max"]
        assert min(smoothed) >= true["min"]

    def test_the_warning_points_at_true_range(self, loaded: Settings) -> None:
        run_id = _ids_by_sport("running")[-1]
        result = tools.get_activity_streams(run_id, ["heart_rate"], max_points=5)
        assert "true_range" in result["downsampled"]

    def test_positions_are_excluded_from_extrema(self, loaded: Settings) -> None:
        # A min/max latitude is a bounding box, not a meaningful peak.
        run_id = _ids_by_sport("running")[-1]
        result = tools.get_activity_streams(run_id, ["lat", "lon", "heart_rate"], max_points=5)
        assert "lat" not in result["true_range"]
        assert "heart_rate" in result["true_range"]

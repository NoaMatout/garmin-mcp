"""Tests for the Garmin layer.

No network here. A fake `ActivitySource` stands in for Garmin, which is exactly
what the interface was introduced for: the sync logic — watermarks, skipping
what we already hold, behaving when the service says no — is the part that can
break silently, and it is fully testable without credentials.

The real backends are covered only where they can be: protocol conformance and
the mapping from library exceptions to ours. Their network calls are verified
by hand against a live account.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from garmin_mcp.config import Backend, Settings
from garmin_mcp.db.migrations import init_database
from garmin_mcp.domain.models import ActivityStub
from garmin_mcp.errors import GarminAuthError, GarminError, GarminRateLimitError
from garmin_mcp.garmin.factory import build_source, resolve_source
from garmin_mcp.garmin.source import ActivitySource, SourceHealth, SourceStatus
from garmin_mcp.ingest.pipeline import sync_from_garmin
from tests import fit_builder

START = datetime(2026, 3, 15, 7, 30, tzinfo=UTC)


class FakeSource:
    """A Garmin that does exactly what a test tells it to."""

    name = "fake"

    def __init__(
        self,
        stubs: list[ActivityStub] | None = None,
        *,
        payloads: dict[int, bytes] | None = None,
        health: SourceHealth = SourceHealth.OK,
        raise_on_download: Exception | None = None,
    ) -> None:
        self.stubs = stubs or []
        self.payloads = payloads or {}
        self.health = health
        self.raise_on_download = raise_on_download
        self.downloaded: list[int] = []
        self.list_calls: list[date | None] = []
        self.closed = False

    def health_check(self) -> SourceStatus:
        return SourceStatus(self.health, self.name)

    def list_activities(
        self,
        since: date | None = None,
        limit: int = 25,
        activity_type: str | None = None,
    ) -> list[ActivityStub]:
        self.list_calls.append(since)
        stubs = self.stubs
        if since is not None:
            stubs = [s for s in stubs if s.start_time_utc.date() >= since]
        return stubs[:limit]

    def download_fit(self, activity_id: int) -> bytes:
        if self.raise_on_download is not None:
            raise self.raise_on_download
        self.downloaded.append(activity_id)
        return self.payloads.get(activity_id) or fit_builder.build_run()

    def close(self) -> None:
        self.closed = True


def _stub(activity_id: int, offset_days: int = 0, sport: str = "running") -> ActivityStub:
    return ActivityStub(
        activity_id=activity_id,
        start_time_utc=START + timedelta(days=offset_days),
        sport=sport,
        name=f"Activity {activity_id}",
    )


@pytest.fixture
def db(settings: Settings) -> Settings:
    init_database(settings)
    return settings


def _activity_count(settings: Settings) -> int:
    with duckdb.connect(str(settings.db_path), read_only=True) as conn:
        return conn.execute("SELECT count(*) FROM activities").fetchone()[0]


class TestInterfaceConformance:
    def test_the_fake_satisfies_the_protocol(self) -> None:
        # If this fails the fake has drifted and every test below is fiction.
        assert isinstance(FakeSource(), ActivitySource)

    def test_both_real_backends_satisfy_the_protocol(self, settings: Settings) -> None:
        assert isinstance(build_source(Backend.CFFI, settings), ActivitySource)
        assert isinstance(build_source(Backend.PLAYWRIGHT, settings), ActivitySource)

    def test_building_a_backend_touches_no_network(self, settings: Settings) -> None:
        # Construction must stay free of I/O so the worker can be built offline.
        source = build_source(Backend.CFFI, settings)
        assert source.name == "cffi"


class TestSync:
    def test_downloads_and_ingests_new_activities(self, db: Settings) -> None:
        source = FakeSource(
            [_stub(1001, 0), _stub(1002, 1)],
            payloads={
                1001: fit_builder.build_run(start=START),
                1002: fit_builder.build_run(start=START + timedelta(days=1)),
            },
        )
        report = sync_from_garmin(source, db)

        assert report.imported == 2
        assert sorted(source.downloaded) == [1001, 1002]
        assert _activity_count(db) == 2

    def test_second_sync_downloads_nothing(self, db: Settings) -> None:
        payloads = {
            1001: fit_builder.build_run(start=START),
            1002: fit_builder.build_run(start=START + timedelta(days=1)),
        }
        stubs = [_stub(1001, 0), _stub(1002, 1)]

        sync_from_garmin(FakeSource(stubs, payloads=payloads), db)
        second = FakeSource(stubs, payloads=payloads)
        report = sync_from_garmin(second, db)

        assert second.downloaded == []
        assert report.imported == 0
        assert _activity_count(db) == 2

    def test_only_the_new_activity_is_fetched(self, db: Settings) -> None:
        payloads = {
            1001: fit_builder.build_run(start=START),
            1002: fit_builder.build_run(start=START + timedelta(days=1)),
        }
        sync_from_garmin(FakeSource([_stub(1001, 0)], payloads=payloads), db)

        source = FakeSource([_stub(1001, 0), _stub(1002, 1)], payloads=payloads)
        sync_from_garmin(source, db)

        assert source.downloaded == [1002]

    def test_the_watermark_follows_the_newest_garmin_activity(self, db: Settings) -> None:
        source = FakeSource([_stub(1001, 0)], payloads={1001: fit_builder.build_run(start=START)})
        sync_from_garmin(source, db)

        second = FakeSource([_stub(1002, 5)])
        sync_from_garmin(second, db)

        assert second.list_calls[-1] == START.date()

    def test_a_manual_import_does_not_move_the_watermark(self, db: Settings) -> None:
        """A 2019 file dropped in the inbox must not convince the sync it is current.

        The watermark counts Garmin-sourced rows only; conflating the two would
        make the next sync skip everything recorded since.
        """
        from garmin_mcp.ingest.pipeline import import_inbox

        (db.inbox_dir / "old.fit").write_bytes(
            fit_builder.build_run(start=datetime(2019, 5, 1, 8, 0, tzinfo=UTC))
        )
        import_inbox(db)

        source = FakeSource([_stub(1001, 0)])
        sync_from_garmin(source, db)
        assert source.list_calls[-1] is None  # no Garmin rows yet, so no watermark

    def test_activities_are_ingested_oldest_first(self, db: Settings) -> None:
        # An interrupted run must leave a contiguous history, not a hole.
        source = FakeSource(
            [_stub(1003, 2), _stub(1001, 0), _stub(1002, 1)],
            payloads={
                1001: fit_builder.build_run(start=START),
                1002: fit_builder.build_run(start=START + timedelta(days=1)),
                1003: fit_builder.build_run(start=START + timedelta(days=2)),
            },
        )
        sync_from_garmin(source, db)
        assert source.downloaded == [1001, 1002, 1003]

    def test_explicit_since_overrides_the_watermark(self, db: Settings) -> None:
        source = FakeSource([_stub(1001, 0)])
        sync_from_garmin(source, db, since=date(2020, 1, 1))
        assert source.list_calls[-1] == date(2020, 1, 1)


class TestSyncFailureHandling:
    def test_rate_limiting_stops_the_run_instead_of_hammering(self, db: Settings) -> None:
        source = FakeSource(
            [_stub(1001, 0), _stub(1002, 1)],
            raise_on_download=GarminRateLimitError("slow down"),
        )
        report = sync_from_garmin(source, db)

        assert report.failed == 1  # stopped after the first refusal
        assert len(report.outcomes) == 1

    def test_one_failed_download_does_not_abort_the_rest(self, db: Settings) -> None:
        calls: list[int] = []

        class Flaky(FakeSource):
            def download_fit(self, activity_id: int) -> bytes:
                calls.append(activity_id)
                if activity_id == 1001:
                    raise GarminError("transient server error")
                return fit_builder.build_run(start=START + timedelta(days=1))

        report = sync_from_garmin(Flaky([_stub(1001, 0), _stub(1002, 1)]), db)

        assert calls == [1001, 1002]
        assert report.failed == 1
        assert report.imported == 1

    def test_an_empty_remote_is_not_an_error(self, db: Settings) -> None:
        report = sync_from_garmin(FakeSource([]), db)
        assert report.outcomes == []
        assert report.failed == 0


class TestBackendSelection:
    def test_auto_falls_back_to_the_browser_when_blocked(self, settings: Settings) -> None:
        """Cloudflare refusing the HTTP client is exactly what the browser fixes."""
        blocked = FakeSource(health=SourceHealth.BLOCKED)
        healthy = FakeSource(health=SourceHealth.OK)
        healthy.name = "playwright"

        import garmin_mcp.garmin.factory as factory

        original = factory.build_source
        factory.build_source = lambda _backend=None, _settings=None: blocked  # type: ignore[assignment]
        import garmin_mcp.garmin.playwright_source as playwright_module

        original_class = playwright_module.PlaywrightSource
        playwright_module.PlaywrightSource = lambda _settings: healthy  # type: ignore[assignment]
        try:
            source, status = resolve_source(settings, backend=Backend.AUTO)
        finally:
            factory.build_source = original  # type: ignore[assignment]
            playwright_module.PlaywrightSource = original_class  # type: ignore[assignment]

        assert source is healthy
        assert status.usable
        assert blocked.closed

    def test_auto_does_not_launch_a_browser_for_a_missing_login(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No backend can invent a login the user has not performed.

        Falling back here would swap a clear "run `garmin-mcp auth`" for a slow,
        confusing browser failure — and start Chromium for nothing.
        """
        import garmin_mcp.garmin.factory as factory

        monkeypatch.setattr(
            factory,
            "build_source",
            lambda *_a, **_k: FakeSource(health=SourceHealth.NEEDS_AUTH),
        )

        def explode(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("the browser must not be started for NEEDS_AUTH")

        import garmin_mcp.garmin.playwright_source as playwright_module

        monkeypatch.setattr(playwright_module, "PlaywrightSource", explode)

        with pytest.raises(GarminAuthError):
            resolve_source(settings, backend=Backend.AUTO)

    def test_an_explicit_backend_is_never_swapped(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import garmin_mcp.garmin.factory as factory

        monkeypatch.setattr(
            factory,
            "build_source",
            lambda *_a, **_k: FakeSource(health=SourceHealth.BLOCKED),
        )
        with pytest.raises(GarminError):
            resolve_source(settings, backend=Backend.CFFI)


class TestStatusReporting:
    def test_status_never_leaks_credentials(self, settings: Settings) -> None:
        # This string reaches logs and MCP tool output.
        for backend in (Backend.CFFI, Backend.PLAYWRIGHT):
            described = build_source(backend, settings).health_check().describe()
            assert "password" not in described.lower()
            assert "token=" not in described.lower()

    def test_a_missing_session_reports_needs_auth_rather_than_raising(
        self, settings: Settings
    ) -> None:
        status = build_source(Backend.CFFI, settings).health_check()
        assert status.health is SourceHealth.NEEDS_AUTH
        assert not status.usable

    def test_describe_is_human_readable(self) -> None:
        status = SourceStatus(SourceHealth.OK, "cffi", profile_name="Noa")
        assert status.describe() == "cffi: ok as Noa"


class TestErrorTranslation:
    def test_missing_credentials_becomes_an_auth_error(self) -> None:
        import garminconnect

        from garmin_mcp.garmin.cffi_source import _translate

        translated = _translate(
            garminconnect.GarminConnectAuthenticationError("Username and password are required")
        )
        assert isinstance(translated, GarminAuthError)
        assert "garmin-mcp auth" in (translated.hint or "")

    def test_rate_limiting_is_distinguished_from_a_refusal(self) -> None:
        import garminconnect

        from garmin_mcp.garmin.cffi_source import _translate

        assert isinstance(
            _translate(garminconnect.GarminConnectTooManyRequestsError("429")),
            GarminRateLimitError,
        )

    def test_a_cloudflare_refusal_suggests_the_fallback(self) -> None:
        from garmin_mcp.errors import GarminBlockedError
        from garmin_mcp.garmin.cffi_source import _translate

        translated = _translate(RuntimeError("HTTP 403 forbidden by cloudflare"))
        assert isinstance(translated, GarminBlockedError)
        assert "inbox" in (translated.hint or "").lower()

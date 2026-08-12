"""Fallback backend: a real headless Chromium.

The premise is simple. Garmin blocks clients by their TLS handshake, and the
one client whose handshake is guaranteed to look like Chrome's is Chrome. So
this backend logs in once in a visible browser — where a human can solve MFA
and any challenge Garmin throws — saves the resulting cookies, and afterwards
issues every API call as `fetch()` executed *inside* a real page. Same origin,
same cookies, same fingerprint as a person clicking around the site.

The cost is real: roughly a gigabyte of Chromium in the image and a browser
process per sync. That is why it is the fallback and not the default, and why
it lives behind an optional extra (`uv sync --extra playwright`).

⚠️ Verification status: the code paths here have not been exercised against a
live Garmin account — that needs credentials the author of this module does
not have. The structure, error mapping and interface conformance are tested;
the network calls are not. Treat the endpoint paths below as the most likely
shape rather than confirmed fact, and expect to adjust them on first run.
"""

from __future__ import annotations

import base64
import contextlib
import json
from datetime import UTC, date, datetime
from typing import Any

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.domain.models import ActivityStub
from garmin_mcp.errors import GarminAuthError, GarminBlockedError, GarminError
from garmin_mcp.garmin.source import SourceHealth, SourceStatus
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

BACKEND_NAME = "playwright"

CONNECT_ORIGIN = "https://connect.garmin.com"
SIGNIN_URL = f"{CONNECT_ORIGIN}/signin"
# Same-origin paths, so the browser attaches its session automatically.
LIST_PATH = "/activitylist-service/activities/search/activities"
DOWNLOAD_PATH = "/download-service/files/activity"

STATE_FILENAME = "playwright_state.json"

# Runs inside the page. Returns the response body as base64 so binary payloads
# survive the trip back to Python unmangled.
_FETCH_SCRIPT = """
async ({ url, binary }) => {
  const response = await fetch(url, {
    credentials: 'include',
    headers: { 'NK': 'NT', 'X-Requested-With': 'XMLHttpRequest' },
  });
  if (!response.ok) {
    return { ok: false, status: response.status,
             body: (await response.text()).slice(0, 500) };
  }
  if (binary) {
    const buffer = await response.arrayBuffer();
    let raw = '';
    const bytes = new Uint8Array(buffer);
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      raw += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return { ok: true, status: response.status, b64: btoa(raw) };
  }
  return { ok: true, status: response.status, body: await response.text() };
}
"""


def _require_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise GarminError(
            "the playwright backend is not installed",
            hint="run `uv sync --extra playwright` then `uv run playwright install chromium`",
        ) from exc
    return sync_playwright


class PlaywrightSource:
    """Drives Garmin Connect through a genuine browser session."""

    name = BACKEND_NAME

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None

    @property
    def state_path(self):  # type: ignore[no-untyped-def]
        return self._settings.token_dir / STATE_FILENAME

    # ─── session ──────────────────────────────────────────────────────

    def _open(self) -> Any:
        """Start a headless browser carrying the saved session."""
        if self._page is not None:
            return self._page

        if not self.state_path.exists():
            raise GarminAuthError(
                f"no saved browser session at {self.state_path}",
                hint="run `garmin-mcp auth --backend playwright` to log in once",
            )

        sync_playwright = _require_playwright()
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        context = self._browser.new_context(storage_state=str(self.state_path))
        self._page = context.new_page()

        # Land on the site first: the fetches below are same-origin and only
        # carry the session once the page's origin matches.
        self._page.goto(CONNECT_ORIGIN, wait_until="domcontentloaded")
        if "signin" in self._page.url or "sso" in self._page.url:
            self.close()
            raise GarminAuthError("the saved browser session has expired")
        return self._page

    def close(self) -> None:
        for closer in (self._browser, self._playwright):
            if closer is None:
                continue
            # Teardown must never mask the error that led us here.
            with contextlib.suppress(Exception):
                closer.close() if hasattr(closer, "close") else closer.stop()
        self._page = self._browser = self._playwright = None

    def _fetch(self, path: str, *, binary: bool = False) -> Any:
        page = self._open()
        result = page.evaluate(_FETCH_SCRIPT, {"url": f"{CONNECT_ORIGIN}{path}", "binary": binary})

        if not result.get("ok"):
            status = result.get("status")
            body = result.get("body", "")
            if status in (401, 403):
                if "cloudflare" in body.lower() or status == 403:
                    raise GarminBlockedError(f"Garmin refused the request (HTTP {status})")
                raise GarminAuthError(f"session rejected (HTTP {status})")
            raise GarminError(f"Garmin returned HTTP {status}: {body[:200]}")

        return base64.b64decode(result["b64"]) if binary else result["body"]

    # ─── interactive login (CLI only) ─────────────────────────────────

    def login_interactive(self, timeout_s: int = 300) -> None:
        """Open a visible browser and wait for the user to sign in.

        Everything hard about Garmin's auth — MFA, consent screens, whatever
        anti-bot challenge is current — is delegated to the human, once. Only
        the resulting cookies are kept.
        """
        sync_playwright = _require_playwright()
        self._settings.token_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(SIGNIN_URL)

            log.info("garmin.awaiting_manual_login", timeout_s=timeout_s)
            try:
                # Signed in once the SSO flow hands control back to the app.
                page.wait_for_url(
                    lambda url: "signin" not in url and "sso" not in url,
                    timeout=timeout_s * 1000,
                )
            except Exception as exc:
                browser.close()
                raise GarminAuthError(f"login was not completed within {timeout_s}s") from exc

            context.storage_state(path=str(self.state_path))
            browser.close()

        self.state_path.chmod(0o600)
        log.info("garmin.browser_session_saved", path=str(self.state_path))

    # ─── ActivitySource ───────────────────────────────────────────────

    def health_check(self) -> SourceStatus:
        try:
            self._open()
        except GarminAuthError as exc:
            return SourceStatus(SourceHealth.NEEDS_AUTH, self.name, exc.message)
        except GarminBlockedError as exc:
            return SourceStatus(SourceHealth.BLOCKED, self.name, exc.message)
        except Exception as exc:
            return SourceStatus(SourceHealth.UNAVAILABLE, self.name, str(exc))
        return SourceStatus(SourceHealth.OK, self.name)

    def list_activities(
        self,
        since: date | None = None,
        limit: int = 25,
        activity_type: str | None = None,
    ) -> list[ActivityStub]:
        from garmin_mcp.garmin.cffi_source import _to_stub

        query = f"?start=0&limit={max(1, min(limit, 100))}"
        if since is not None:
            query += f"&startDate={since.isoformat()}"
        if activity_type:
            query += f"&activityType={activity_type}"

        body = self._fetch(LIST_PATH + query)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GarminError("Garmin returned a non-JSON activity list") from exc

        if isinstance(payload, dict):
            payload = payload.get("activityList", [])

        stubs = [stub for entry in payload if (stub := _to_stub(entry)) is not None]
        stubs.sort(key=lambda s: s.start_time_utc, reverse=True)
        log.info("garmin.listed", backend=self.name, count=len(stubs))
        return stubs[:limit]

    def download_fit(self, activity_id: int) -> bytes:
        # _fetch is typed Any because it returns text or bytes by flag.
        data: bytes = self._fetch(f"{DOWNLOAD_PATH}/{activity_id}", binary=True)
        if not data:
            raise GarminError(f"Garmin returned an empty file for activity {activity_id}")
        return data


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=UTC)

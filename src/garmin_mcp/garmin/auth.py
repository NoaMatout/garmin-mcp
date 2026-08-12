"""Interactive authentication — CLI only.

Deliberately isolated from everything the server and the worker touch. This is
the one place a password is read and an MFA code is asked for, and it only ever
runs with a human present.

The rest of the system consumes the token this leaves behind. That separation
is what lets the MCP server keep answering questions from the database when
the Garmin session dies, instead of hanging on a prompt nobody will see.

The password is never written to disk, never logged, and never stored in the
database — only the resulting OAuth token is persisted, with file permissions
tightened afterwards.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garmin_mcp.config import Backend, Settings, get_settings
from garmin_mcp.errors import GarminAuthError, GarminError
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

MfaPrompt = Callable[[], str]


def _secure(settings: Settings) -> None:
    """Make the token directory readable only by its owner."""
    try:
        settings.token_dir.chmod(0o700)
        for path in settings.token_dir.iterdir():
            if path.is_file():
                path.chmod(0o600)
    except OSError as exc:  # pragma: no cover - platform dependent
        log.warning("auth.could_not_tighten_permissions", error=str(exc))


def has_session(settings: Settings | None = None) -> bool:
    """Whether any saved credential material exists (without validating it)."""
    settings = settings or get_settings()
    if not settings.token_dir.is_dir():
        return False
    return any(settings.token_dir.iterdir())


def login_cffi(
    settings: Settings | None = None,
    *,
    email: str | None = None,
    password: str | None = None,
    mfa_prompt: MfaPrompt | None = None,
) -> str | None:
    """Log in over HTTP and persist the token. Returns the profile name.

    Credentials come from the environment by default so they never appear in
    shell history or in a process listing.
    """
    settings = settings or get_settings()
    email = email or settings.email
    password = password or (settings.password.get_secret_value() if settings.password else None)

    if not email or not password:
        raise GarminAuthError(
            "no Garmin credentials available",
            hint="set GARMIN_EMAIL and GARMIN_PASSWORD in .env "
            "(copy .env.example), or pass --email",
        )

    try:
        import garminconnect
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise GarminError("garminconnect is not installed; run `uv sync`") from exc

    settings.token_dir.mkdir(parents=True, exist_ok=True)

    client: Any = garminconnect.Garmin(
        email=email,
        password=password,
        prompt_mfa=mfa_prompt or (lambda: ""),
    )

    try:
        client.login(str(settings.token_dir))
    except Exception as exc:
        from garmin_mcp.garmin.cffi_source import _translate

        raise _translate(exc) from exc
    finally:
        # Drop the password from memory as soon as it has served its purpose.
        password = None

    _secure(settings)
    profile = getattr(client, "display_name", None) or getattr(client, "full_name", None)
    log.info("auth.login_succeeded", backend="cffi", profile=profile)
    return profile


def login_playwright(settings: Settings | None = None, *, timeout_s: int = 300) -> None:
    """Open a real browser and let the user sign in by hand.

    The right answer when Garmin blocks scripted clients outright: whatever
    challenge it presents, a human in a real browser can pass it.
    """
    settings = settings or get_settings()
    from garmin_mcp.garmin.playwright_source import PlaywrightSource

    source = PlaywrightSource(settings)
    source.login_interactive(timeout_s=timeout_s)
    _secure(settings)


def login(
    backend: Backend,
    settings: Settings | None = None,
    *,
    email: str | None = None,
    password: str | None = None,
    mfa_prompt: MfaPrompt | None = None,
    timeout_s: int = 300,
) -> str | None:
    """Authenticate with the requested backend.

    `auto` means "try the cheap path, and only inconvenience the user with a
    browser if that path is refused" — the same policy the factory applies at
    read time.
    """
    settings = settings or get_settings()

    if backend is Backend.PLAYWRIGHT:
        login_playwright(settings, timeout_s=timeout_s)
        return None

    try:
        return login_cffi(settings, email=email, password=password, mfa_prompt=mfa_prompt)
    except GarminError as exc:
        if backend is not Backend.AUTO:
            raise
        log.warning("auth.cffi_failed_trying_browser", error=str(exc))
        login_playwright(settings, timeout_s=timeout_s)
        return None

"""Save Page Now configuration and Internet Archive S3 credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ACCESS_KEY_ENV = "SAVEPAGENOW_ACCESS_KEY"
ANONYMOUS_CAPTURES_PER_MINUTE = 6
ANONYMOUS_DAILY_CAP = 4000
SECRET_KEY_ENV = "SAVEPAGENOW_SECRET_KEY"
LOGGED_IN_SIG_ENV = "SAVEPAGENOW_LOGGED_IN_SIG"
LOGGED_IN_USER_ENV = "SAVEPAGENOW_LOGGED_IN_USER"
PREFER_COOKIES_ENV = "SAVEPAGENOW_PREFER_COOKIES"

SpnAuthMethod = Literal["s3", "cookies"]

AUTHENTICATED_CAPTURES_PER_MINUTE = 7
AUTHENTICATED_DAILY_CAP = 100_000
IF_NOT_ARCHIVED_WITHIN = "30m"

# Per-host concurrency limits.  IA allows 12 concurrent pending jobs per user
# globally, but having too many concurrent captures on the *same host* triggers
# IA's "host overloaded" throttling.  Since arkivari archives one domain at a
# time, keep these conservative.
ANONYMOUS_MAX_CONCURRENT = 6
AUTHENTICATED_MAX_CONCURRENT = 7

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PARENT_WALK_MAX = 5
_SPN_ENV_KEYS = frozenset(
    {
        ACCESS_KEY_ENV,
        SECRET_KEY_ENV,
        LOGGED_IN_SIG_ENV,
        LOGGED_IN_USER_ENV,
        PREFER_COOKIES_ENV,
    }
)

# Toggle Save Page Now capture options (same names as the web.archive.org/save form).
# Screenshot, my web archive, email, and WACZ require an Internet Archive account (S3 keys).
SPN_CAPTURE_OPTIONS: dict[str, bool] = {
    "capture_all": True,  # Save error pages (HTTP 4xx, 5xx)
    "capture_outlinks": False,  # Save outlinks (up to 100 per page) — WARNING: causes IA to spawn up to 100 extra captures per URL on the same host, easily triggering per-host throttling
    "capture_screenshot": False,
    "disable_adblocker": False,
    "wm-save-mywebarchive": False,  # Save also in my web archive
    "email_result": False,  # Email me the results
    "wacz": False,  # Email me a WACZ file with the results
}


def _default_capture_options() -> dict[str, bool]:
    return dict(SPN_CAPTURE_OPTIONS)


def spn_capture_post_fields(capture_options: Mapping[str, bool]) -> dict[str, str]:
    """Form fields for enabled SPN options (value ``1`` when True)."""
    return {name: "1" for name, enabled in capture_options.items() if enabled}


@dataclass(frozen=True)
class SpnConfig:
    authenticated: bool
    access_key: str | None
    secret_key: str | None
    captures_per_minute: int
    daily_cap: int
    min_interval_seconds: float
    auth_method: SpnAuthMethod | None = None
    logged_in_sig: str | None = None
    logged_in_user: str | None = None
    if_not_archived_within: str = IF_NOT_ARCHIVED_WITHIN
    max_concurrent: int = ANONYMOUS_MAX_CONCURRENT
    capture_options: dict[str, bool] = field(default_factory=_default_capture_options)

    @property
    def mode_label(self) -> str:
        if not self.authenticated:
            return "anonymous"
        if self.auth_method == "cookies":
            return "authenticated (cookies)"
        return "authenticated"


def build_spn_submit_data(url: str, spn_config: SpnConfig, *, authenticated: bool) -> dict[str, str]:
    """POST body for a Save Page Now capture request."""
    data: dict[str, str] = {"url": url, **spn_capture_post_fields(spn_config.capture_options)}
    if authenticated:
        data["skip_first_archive"] = "1"
        data["if_not_archived_within"] = spn_config.if_not_archived_within
    return data


def discover_dotenv_path(explicit: Path | None = None) -> Path | None:
    """
    Locate a `.env` file for credential loading.

    Order: explicit path, ``cwd/.env``, package repo root, then up to five parents of cwd.
    """
    if explicit is not None:
        return explicit if explicit.is_file() else None

    candidates: list[Path] = [
        Path.cwd() / ".env",
        _PACKAGE_ROOT / ".env",
    ]
    current = Path.cwd()
    for _ in range(_DOTENV_PARENT_WALK_MAX):
        current = current.parent
        if not current or current == current.parent:
            break
        candidates.append(current / ".env")

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            return candidate
    return None


def _parse_dotenv_file(env_path: Path) -> dict[str, str]:
    try:
        from dotenv import dotenv_values

        raw = dotenv_values(env_path)
        return {key: value for key, value in raw.items() if key and value is not None}
    except ImportError:
        pass

    parsed: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            parsed[key] = value
    return parsed


def load_dotenv(path: Path | None = None) -> None:
    """
    Load environment variables from a `.env` file into ``os.environ``.

    ``SAVEPAGENOW_*`` keys from the file always override existing shell values.
    Other keys are set only when not already in the environment.
    """
    env_path = discover_dotenv_path(path)
    if env_path is None:
        return

    for key, value in _parse_dotenv_file(env_path).items():
        if key in _SPN_ENV_KEYS:
            os.environ[key] = value
        elif key not in os.environ:
            os.environ[key] = value


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_spn_config(force_anonymous: bool = False) -> SpnConfig:
    """Build SPN config from environment (call load_dotenv first)."""
    if force_anonymous:
        return _anonymous_config()

    access_key = os.environ.get(ACCESS_KEY_ENV, "").strip()
    secret_key = os.environ.get(SECRET_KEY_ENV, "").strip()
    logged_in_sig = os.environ.get(LOGGED_IN_SIG_ENV, "").strip()
    logged_in_user = os.environ.get(LOGGED_IN_USER_ENV, "").strip()
    has_s3 = bool(access_key and secret_key)
    has_cookies = bool(logged_in_sig and logged_in_user)

    if has_s3 and has_cookies and not _env_truthy(PREFER_COOKIES_ENV):
        return _authenticated_config(
            "s3",
            access_key=access_key,
            secret_key=secret_key,
            logged_in_sig=logged_in_sig,
            logged_in_user=logged_in_user,
        )
    if has_cookies and (not has_s3 or _env_truthy(PREFER_COOKIES_ENV)):
        return _authenticated_config(
            "cookies",
            access_key=access_key if access_key else None,
            secret_key=secret_key if secret_key else None,
            logged_in_sig=logged_in_sig,
            logged_in_user=logged_in_user,
        )
    if has_s3:
        return _authenticated_config(
            "s3",
            access_key=access_key,
            secret_key=secret_key,
        )
    return _anonymous_config()


def _authenticated_config(
    auth_method: SpnAuthMethod,
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
    logged_in_sig: str | None = None,
    logged_in_user: str | None = None,
) -> SpnConfig:
    interval = 60.0 / AUTHENTICATED_CAPTURES_PER_MINUTE
    return SpnConfig(
        authenticated=True,
        auth_method=auth_method,
        access_key=access_key,
        secret_key=secret_key,
        logged_in_sig=logged_in_sig,
        logged_in_user=logged_in_user,
        captures_per_minute=AUTHENTICATED_CAPTURES_PER_MINUTE,
        daily_cap=AUTHENTICATED_DAILY_CAP,
        min_interval_seconds=interval,
        max_concurrent=AUTHENTICATED_MAX_CONCURRENT,
        capture_options=dict(SPN_CAPTURE_OPTIONS),
    )


def _anonymous_config() -> SpnConfig:
    return SpnConfig(
        authenticated=False,
        auth_method=None,
        access_key=None,
        secret_key=None,
        logged_in_sig=None,
        logged_in_user=None,
        captures_per_minute=ANONYMOUS_CAPTURES_PER_MINUTE,
        daily_cap=ANONYMOUS_DAILY_CAP,
        min_interval_seconds=60.0 / ANONYMOUS_CAPTURES_PER_MINUTE,
        capture_options=dict(SPN_CAPTURE_OPTIONS),
    )


def authorization_header(config: SpnConfig) -> str:
    if config.auth_method != "s3" or not config.access_key or not config.secret_key:
        raise ValueError("authorization_header requires SpnConfig with auth_method s3 and keys")
    return f"LOW {config.access_key}:{config.secret_key}"


def cookie_header(config: SpnConfig) -> str:
    if config.auth_method != "cookies" or not config.logged_in_sig or not config.logged_in_user:
        raise ValueError("cookie_header requires SpnConfig with auth_method cookies")
    return f"logged-in-sig={config.logged_in_sig}; logged-in-user={config.logged_in_user}"


def apply_spn_auth_headers(headers: dict[str, str], config: SpnConfig) -> None:
    """Add S3 ``Authorization`` or session ``Cookie`` per ``config.auth_method``."""
    if config.auth_method == "s3":
        headers["Authorization"] = authorization_header(config)
    elif config.auth_method == "cookies":
        headers["Cookie"] = cookie_header(config)


def resolve_daily_limit(
    cli_daily_limit: int | None,
    spn_config: SpnConfig,
    queue_daily_limit: int | None = None,
    user_set_daily_limit: bool = False,
) -> int:
    """
    Pick effective UTC daily archive cap.

    user_set_daily_limit is True when the user passed --daily-limit on the CLI.
    """
    if user_set_daily_limit and cli_daily_limit is not None:
        return cli_daily_limit
    if queue_daily_limit is not None:
        if spn_config.authenticated and queue_daily_limit == ANONYMOUS_DAILY_CAP:
            return AUTHENTICATED_DAILY_CAP
        return queue_daily_limit
    return spn_config.daily_cap
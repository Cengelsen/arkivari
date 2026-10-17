"""Save Page Now configuration and Internet Archive S3 credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ACCESS_KEY_ENV = "SAVEPAGENOW_ACCESS_KEY"
ANONYMOUS_CAPTURES_PER_MINUTE = 6
ANONYMOUS_DAILY_CAP = 4000
SECRET_KEY_ENV = "SAVEPAGENOW_SECRET_KEY"

AUTHENTICATED_CAPTURES_PER_MINUTE = 7
AUTHENTICATED_DAILY_CAP = 100_000
IF_NOT_ARCHIVED_WITHIN = "30m"

# Per-host concurrency limits.  IA allows 12 concurrent pending jobs per user
# globally, but having too many concurrent captures on the *same host* triggers
# IA's "host overloaded" throttling.  Since arkivari archives one domain at a
# time, keep these conservative.
ANONYMOUS_MAX_CONCURRENT = 6
AUTHENTICATED_MAX_CONCURRENT = 7

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
    if_not_archived_within: str = IF_NOT_ARCHIVED_WITHIN
    max_concurrent: int = ANONYMOUS_MAX_CONCURRENT
    capture_options: dict[str, bool] = field(default_factory=_default_capture_options)

    @property
    def mode_label(self) -> str:
        return "authenticated" if self.authenticated else "anonymous"


def build_spn_submit_data(url: str, spn_config: SpnConfig, *, authenticated: bool) -> dict[str, str]:
    """POST body for a Save Page Now capture request."""
    data: dict[str, str] = {"url": url, **spn_capture_post_fields(spn_config.capture_options)}
    if authenticated:
        data["skip_first_archive"] = "1"
        data["if_not_archived_within"] = spn_config.if_not_archived_within
    return data


def load_dotenv(path: Path | None = None) -> None:
    """Load environment variables from a .env file into os.environ (no override)."""
    env_path = path or Path.cwd() / ".env"
    if not env_path.is_file():
        return

    try:
        from dotenv import load_dotenv as _dotenv_load

        _dotenv_load(env_path, override=False)
        return
    except ImportError:
        pass

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_spn_config(force_anonymous: bool = False) -> SpnConfig:
    """Build SPN config from environment (call load_dotenv first)."""
    if force_anonymous:
        return _anonymous_config()

    access_key = os.environ.get(ACCESS_KEY_ENV, "").strip()
    secret_key = os.environ.get(SECRET_KEY_ENV, "").strip()
    if access_key and secret_key:
        interval = 60.0 / AUTHENTICATED_CAPTURES_PER_MINUTE
        return SpnConfig(
            authenticated=True,
            access_key=access_key,
            secret_key=secret_key,
            captures_per_minute=AUTHENTICATED_CAPTURES_PER_MINUTE,
            daily_cap=AUTHENTICATED_DAILY_CAP,
            min_interval_seconds=interval,
            max_concurrent=AUTHENTICATED_MAX_CONCURRENT,
            capture_options=dict(SPN_CAPTURE_OPTIONS),
        )
    return _anonymous_config()


def _anonymous_config() -> SpnConfig:
    return SpnConfig(
        authenticated=False,
        access_key=None,
        secret_key=None,
        captures_per_minute=ANONYMOUS_CAPTURES_PER_MINUTE,
        daily_cap=ANONYMOUS_DAILY_CAP,
        min_interval_seconds=60.0 / ANONYMOUS_CAPTURES_PER_MINUTE,
        capture_options=dict(SPN_CAPTURE_OPTIONS),
    )


def authorization_header(config: SpnConfig) -> str:
    if not config.authenticated or not config.access_key or not config.secret_key:
        raise ValueError("authorization_header requires authenticated SpnConfig with keys")
    return f"LOW {config.access_key}:{config.secret_key}"


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

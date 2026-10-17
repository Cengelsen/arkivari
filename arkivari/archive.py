"""Internet Archive Save Page Now integration."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Literal

import requests

from arkivari.rate_limiter import CrawlRateLimiter
from arkivari.spn_config import (
    SPN_CAPTURE_OPTIONS,
    SpnConfig,
    authorization_header,
    build_spn_submit_data,
)

logger = logging.getLogger(__name__)

SAVE_PAGE_URL = "https://web.archive.org/save/"
SAVE_PAGE_API = "https://web.archive.org/save"
WAYBACK_BASE = "https://web.archive.org"

ANONYMOUS_CAPTURES_PER_MINUTE = 3
ANONYMOUS_DAILY_CAP = 4000
MIN_INTERVAL_SECONDS = 60.0 / ANONYMOUS_CAPTURES_PER_MINUTE
ESTIMATED_CAPTURE_SECONDS = 15.0
MAX_RETRIES = 5
POLL_INTERVAL_SECONDS = 3.0
# SPN2 max capture duration is ~2m; allow extra time for IA queue delays and poll 429s.
MAX_POLL_SECONDS = 300.0
POLL_429_BACKOFF_SECONDS = 15.0


def estimate_archive_seconds(
    page_count: int,
    min_interval_seconds: float = MIN_INTERVAL_SECONDS,
) -> float:
    """Estimate wall-clock time to archive pages at IA rate limits.

    With concurrent captures, submissions are pipelined at *min_interval_seconds*
    apart.  Polling happens in parallel, so the dominant cost per page is the
    submission interval, not interval + capture time.
    """
    if page_count <= 0:
        return 0.0
    return ESTIMATED_CAPTURE_SECONDS + (page_count - 1) * min_interval_seconds

JOB_ID_PATTERN = re.compile(r'watchJob\("([^"]+)"')
SPN_MESSAGE_PATTERN = re.compile(r'id="spn-message">([^<]+)')

ArchiveStatus = Literal["archived", "cached", "skipped", "failed"]


@dataclass
class ArchiveResult:
    url: str
    status: ArchiveStatus
    archive_url: str | None = None
    error: str | None = None


@dataclass
class ArchiveSummary:
    archived_count: int = 0
    cached_count: int = 0
    skipped_robots: int = 0
    failed_count: int = 0
    results: list[ArchiveResult] = field(default_factory=list)

    def add(self, result: ArchiveResult) -> None:
        self.results.append(result)
        if result.status == "archived":
            self.archived_count += 1
        elif result.status == "cached":
            self.cached_count += 1
        elif result.status == "skipped":
            self.skipped_robots += 1
        elif result.status == "failed":
            self.failed_count += 1


class SpnError(Exception):
    """Save Page Now request failed."""


_MAX_ERROR_LENGTH = 300


def _extract_html_error_message(html: str) -> str | None:
    match = re.search(r'class="error-text"[^>]*>([^<]+)', html)
    if match:
        return unescape(match.group(1).strip())
    message_match = SPN_MESSAGE_PATTERN.search(html)
    if message_match:
        return unescape(message_match.group(1).strip())
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if title_match:
        return unescape(title_match.group(1).strip())
    return None


def _browser_headers(user_agent: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Referer": SAVE_PAGE_URL,
    }
    if extra:
        headers.update(extra)
    return headers


def _build_archive_url(timestamp: str, original_url: str) -> str:
    return f"{WAYBACK_BASE}/web/{timestamp}/{original_url}"


def _format_job_error(payload: dict[str, Any]) -> str:
    message = payload.get("message") or "Capture failed"
    status_ext = payload.get("status_ext")
    if status_ext:
        return f"{status_ext}: {message}"
    return str(message)


def _parse_submit_response(html: str) -> tuple[str | None, str | None]:
    job_match = JOB_ID_PATTERN.search(html)
    message_match = SPN_MESSAGE_PATTERN.search(html)
    job_id = job_match.group(1) if job_match else None
    message = unescape(message_match.group(1).strip()) if message_match else None
    return job_id, message


def _submit_capture_anonymous(
    session: requests.Session,
    url: str,
    user_agent: str,
    spn_config: SpnConfig,
) -> tuple[str | None, str | None]:
    """Submit a capture using the browser's POST + async job flow."""
    response = session.post(
        f"{SAVE_PAGE_URL}{url}",
        data=build_spn_submit_data(url, spn_config, authenticated=False),
        headers=_browser_headers(user_agent),
        timeout=30,
    )

    if response.status_code == 429:
        raise SpnError("Rate limited by Internet Archive (HTTP 429)")

    if response.status_code >= 400:
        message = _extract_html_error_message(response.text)
        if message:
            raise SpnError(f"HTTP {response.status_code}: {message}")
        raise SpnError(f"Internet Archive returned HTTP {response.status_code}")

    job_id, message = _parse_submit_response(response.text)
    if not job_id:
        message = message or _extract_html_error_message(response.text) or "No capture job returned"
        raise SpnError(message)

    return job_id, message


def _submit_capture_authenticated(
    session: requests.Session,
    url: str,
    user_agent: str,
    spn_config: SpnConfig,
) -> tuple[str, str | None]:
    """Submit a capture via SPN2 JSON API."""
    headers = _browser_headers(
        user_agent,
        {
            "Accept": "application/json",
            "Authorization": authorization_header(spn_config),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
    )
    data = build_spn_submit_data(url, spn_config, authenticated=True)
    response = session.post(SAVE_PAGE_API, data=data, headers=headers, timeout=30)

    if response.status_code == 429:
        raise SpnError("Rate limited by Internet Archive (HTTP 429)")

    if response.status_code >= 400:
        snippet = response.text[:200].replace("\n", " ")
        raise SpnError(f"HTTP {response.status_code}: {snippet}")

    try:
        payload = response.json()
    except ValueError as exc:
        snippet = response.text[:200].replace("\n", " ")
        raise SpnError(f"Invalid JSON from Internet Archive: {snippet}") from exc

    job_id = payload.get("job_id")
    if not job_id:
        raise SpnError(f"No capture job returned: {json.dumps(payload)[:_MAX_ERROR_LENGTH]}")

    message = payload.get("message")
    return str(job_id), message if isinstance(message, str) else None


def _retry_after_seconds(response: requests.Response, default: float) -> float:
    raw = response.headers.get("Retry-After")
    if not raw:
        return default
    try:
        return min(max(float(raw.strip()), 1.0), 120.0)
    except ValueError:
        return default


def _poll_capture(
    session: requests.Session,
    job_id: str,
    user_agent: str,
    spn_config: SpnConfig,
    poll_limiter: CrawlRateLimiter | None = None,
) -> dict[str, Any]:
    """Poll job status until success, error, or timeout."""
    deadline = time.monotonic() + MAX_POLL_SECONDS
    poll_headers = _browser_headers(user_agent, {"Accept": "application/json"})
    if spn_config.authenticated:
        poll_headers["Authorization"] = authorization_header(spn_config)

    while time.monotonic() < deadline:
        if poll_limiter is not None:
            poll_limiter.acquire()
        response = session.get(
            f"{SAVE_PAGE_URL}status/{job_id}",
            params={"_t": int(time.time() * 1000)},
            headers=poll_headers,
            timeout=30,
        )

        if response.status_code == 429:
            time.sleep(_retry_after_seconds(response, POLL_429_BACKOFF_SECONDS))
            continue

        if response.status_code >= 400:
            raise SpnError(f"Status poll failed with HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SpnError(f"Invalid status response: {exc}") from exc

        status = payload.get("status")
        if status == "pending":
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        return payload

    raise SpnError("Capture timed out waiting for Internet Archive job")


def _is_cached_message(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return (
        "same snapshot" in lowered
        or "already been archived" in lowered
        or "recent capture" in lowered
    )


def _payload_to_result(
    url: str,
    payload: dict[str, Any],
    submit_message: str | None,
) -> ArchiveResult:
    status = payload.get("status")
    if status == "error":
        error = _format_job_error(payload)
        return ArchiveResult(url=url, status="failed", error=error[:_MAX_ERROR_LENGTH])

    if status != "success":
        return ArchiveResult(
            url=url,
            status="failed",
            error=f"Unexpected job status: {status}",
        )

    timestamp = payload.get("timestamp")
    original_url = payload.get("original_url") or url
    if not timestamp:
        return ArchiveResult(url=url, status="failed", error="Capture succeeded but no timestamp returned")

    archive_url = _build_archive_url(timestamp, original_url)
    job_message = payload.get("message")
    hint = submit_message or (str(job_message) if job_message else None)
    result_status: ArchiveStatus = "cached" if _is_cached_message(hint) else "archived"
    return ArchiveResult(url=url, status=result_status, archive_url=archive_url)


def _run_capture(
    session: requests.Session,
    url: str,
    user_agent: str,
    spn_config: SpnConfig,
    poll_limiter: CrawlRateLimiter | None = None,
) -> ArchiveResult:
    """Archive a URL via POST submit and async status polling."""
    if spn_config.authenticated:
        job_id, submit_message = _submit_capture_authenticated(session, url, user_agent, spn_config)
    else:
        job_id, submit_message = _submit_capture_anonymous(session, url, user_agent, spn_config)
        if not job_id:
            return ArchiveResult(url=url, status="failed", error="No capture job returned")

    payload = _poll_capture(
        session,
        job_id,
        user_agent,
        spn_config,
        poll_limiter,
    )
    return _payload_to_result(url, payload, submit_message)


def archive_url(
    url: str,
    user_agent: str,
    session: requests.Session | None = None,
    spn_config: SpnConfig | None = None,
    poll_limiter: CrawlRateLimiter | None = None,
) -> ArchiveResult:
    """Archive a single URL via Save Page Now.

    Rate limiting of submissions is handled by the caller (the session driver
    acquires a token before dispatching each worker).

    On HTTP 429 the function returns a ``"failed"`` result immediately so
    the session driver can apply a cooldown to the shared rate limiter and
    re-queue the URL.  Only transient network errors are retried here.
    """
    config = spn_config or SpnConfig(
        authenticated=False,
        access_key=None,
        secret_key=None,
        captures_per_minute=ANONYMOUS_CAPTURES_PER_MINUTE,
        daily_cap=ANONYMOUS_DAILY_CAP,
        min_interval_seconds=MIN_INTERVAL_SECONDS,
        capture_options=dict(SPN_CAPTURE_OPTIONS),
    )
    http = session or requests.Session()
    backoff = config.min_interval_seconds

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _run_capture(http, url, user_agent, config, poll_limiter)

        except SpnError as exc:
            message = str(exc)
            if "429" in message or "rate limit" in message.lower():
                logger.warning(
                    "Rate limited archiving %s; returning to queue",
                    url,
                )
                return ArchiveResult(url=url, status="failed", error=message[:_MAX_ERROR_LENGTH])
            return ArchiveResult(url=url, status="failed", error=message[:_MAX_ERROR_LENGTH])

        except requests.exceptions.RequestException as exc:
            if attempt == MAX_RETRIES:
                return ArchiveResult(url=url, status="failed", error=str(exc))
            logger.warning(
                "Network error archiving %s (attempt %d/%d): %s",
                url,
                attempt,
                MAX_RETRIES,
                exc,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)

    return ArchiveResult(url=url, status="failed", error="Max retries exceeded")

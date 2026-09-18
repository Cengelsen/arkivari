"""Internet Archive Save Page Now integration."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Literal

import requests

from arkivari.robots import RobotsPolicy

logger = logging.getLogger(__name__)

SAVE_PAGE_URL = "https://web.archive.org/save/"
WAYBACK_BASE = "https://web.archive.org"

ANONYMOUS_CAPTURES_PER_MINUTE = 3
ANONYMOUS_DAILY_CAP = 4000
MIN_INTERVAL_SECONDS = 60.0 / ANONYMOUS_CAPTURES_PER_MINUTE
MAX_RETRIES = 5
POLL_INTERVAL_SECONDS = 3.0
MAX_POLL_SECONDS = 120.0

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


class ArchiveRateLimiter:
    """Enforce anonymous Save Page Now rate limits."""

    def __init__(self) -> None:
        self._last_capture_at = 0.0
        self._capture_count = 0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_capture_at
        remaining = MIN_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_capture_at = time.monotonic()

    def can_capture(self, max_archives: int) -> bool:
        return self._capture_count < max_archives

    def record_capture(self) -> None:
        self._capture_count += 1


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


def _browser_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Referer": SAVE_PAGE_URL,
    }


def _build_archive_url(timestamp: str, original_url: str) -> str:
    return f"{WAYBACK_BASE}/web/{timestamp}/{original_url}"


def _parse_submit_response(html: str) -> tuple[str | None, str | None]:
    job_match = JOB_ID_PATTERN.search(html)
    message_match = SPN_MESSAGE_PATTERN.search(html)
    job_id = job_match.group(1) if job_match else None
    message = unescape(message_match.group(1).strip()) if message_match else None
    return job_id, message


def _submit_capture(
    session: requests.Session,
    url: str,
    user_agent: str,
) -> tuple[str | None, str | None]:
    """Submit a capture using the browser's POST + async job flow."""
    response = session.post(
        f"{SAVE_PAGE_URL}{url}",
        data={"url": url, "capture_all": "1"},
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


def _poll_capture(
    session: requests.Session,
    job_id: str,
    user_agent: str,
) -> dict[str, Any]:
    """Poll job status until success, error, or timeout."""
    deadline = time.monotonic() + MAX_POLL_SECONDS

    while time.monotonic() < deadline:
        response = session.get(
            f"{SAVE_PAGE_URL}status/{job_id}",
            params={"_t": int(time.time() * 1000)},
            headers=_browser_headers(user_agent),
            timeout=30,
        )

        if response.status_code == 429:
            time.sleep(MIN_INTERVAL_SECONDS)
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
    return "same snapshot" in lowered or "already been archived" in lowered


def _run_capture(
    session: requests.Session,
    url: str,
    user_agent: str,
) -> ArchiveResult:
    """Archive a URL via POST submit and async status polling."""
    job_id, submit_message = _submit_capture(session, url, user_agent)
    payload = _poll_capture(session, job_id, user_agent)

    status = payload.get("status")
    if status == "error":
        error = payload.get("message") or payload.get("status_ext") or "Capture failed"
        return ArchiveResult(url=url, status="failed", error=str(error)[:_MAX_ERROR_LENGTH])

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
    result_status: ArchiveStatus = "cached" if _is_cached_message(submit_message) else "archived"
    return ArchiveResult(url=url, status=result_status, archive_url=archive_url)


def archive_url(
    url: str,
    user_agent: str,
    rate_limiter: ArchiveRateLimiter,
    session: requests.Session | None = None,
    authenticate: bool = False,
) -> ArchiveResult:
    """Archive a single URL via Save Page Now."""
    if authenticate:
        return ArchiveResult(
            url=url,
            status="failed",
            error="Authenticated mode is not implemented yet",
        )

    http = session or requests.Session()
    backoff = MIN_INTERVAL_SECONDS

    for attempt in range(1, MAX_RETRIES + 1):
        rate_limiter.wait()
        try:
            result = _run_capture(http, url, user_agent)
            rate_limiter.record_capture()
            return result

        except SpnError as exc:
            message = str(exc)
            if "429" in message or "rate limit" in message.lower():
                logger.warning(
                    "Rate limited archiving %s (attempt %d/%d); waiting %.1fs",
                    url,
                    attempt,
                    MAX_RETRIES,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
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


def archive_urls(
    urls: list[str],
    robots: RobotsPolicy,
    user_agent: str,
    max_archives: int = ANONYMOUS_DAILY_CAP,
    verbose: bool = False,
    authenticate: bool = False,
) -> ArchiveSummary:
    """Archive discovered URLs while respecting robots.txt and IA limits."""
    summary = ArchiveSummary()
    rate_limiter = ArchiveRateLimiter()
    session = requests.Session()

    for url in urls:
        if not rate_limiter.can_capture(max_archives):
            logger.error(
                "Reached archive cap of %d URLs (anonymous daily limit is %d)",
                max_archives,
                ANONYMOUS_DAILY_CAP,
            )
            break

        if not robots.can_fetch(url):
            result = ArchiveResult(url=url, status="skipped", error="Disallowed by robots.txt")
            summary.add(result)
            if verbose:
                logger.info("Skipped (robots.txt): %s", url)
            continue

        result = archive_url(
            url,
            user_agent,
            rate_limiter,
            session=session,
            authenticate=authenticate,
        )
        summary.add(result)

        if verbose:
            if result.status == "archived":
                logger.info("Archived: %s -> %s", url, result.archive_url)
            elif result.status == "cached":
                logger.info("Already cached: %s -> %s", url, result.archive_url)
            else:
                logger.error("Failed: %s (%s)", url, result.error)

    return summary

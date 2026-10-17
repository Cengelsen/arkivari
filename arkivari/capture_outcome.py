"""Classify Save Page Now results for accurate logging and session metrics."""

from __future__ import annotations

import logging
from enum import Enum

from arkivari.archive import ArchiveResult


class CaptureOutcomeKind(str, Enum):
    """What actually happened to a capture attempt."""

    NEW_CAPTURE = "new_capture"
    REUSED_SNAPSHOT = "reused_snapshot"
    SKIPPED_ROBOTS = "skipped_robots"
    DEFERRED_SUBMIT_RATE_LIMIT = "deferred_submit_rate_limit"
    DEFERRED_POLL_TIMEOUT = "deferred_poll_timeout"
    DEFERRED_HOST_OVERLOAD = "deferred_host_overload"
    DEFERRED_IA_BUSY = "deferred_ia_busy"
    BLOCKED_DAILY_URL_CAP = "blocked_daily_url_cap"
    CAPTURE_ERROR = "capture_error"


DEFERRED_OUTCOMES = frozenset(
    {
        CaptureOutcomeKind.DEFERRED_SUBMIT_RATE_LIMIT,
        CaptureOutcomeKind.DEFERRED_POLL_TIMEOUT,
        CaptureOutcomeKind.DEFERRED_HOST_OVERLOAD,
        CaptureOutcomeKind.DEFERRED_IA_BUSY,
    }
)


def classify_archive_error(error: str | None) -> CaptureOutcomeKind:
    """Map an ``ArchiveResult.error`` string to a capture outcome kind."""
    if not error:
        return CaptureOutcomeKind.CAPTURE_ERROR

    lowered = error.lower()

    if "error:too-many-daily-captures" in lowered:
        return CaptureOutcomeKind.BLOCKED_DAILY_URL_CAP

    if "429" in error or "rate limit" in lowered:
        return CaptureOutcomeKind.DEFERRED_SUBMIT_RATE_LIMIT

    if "timed out waiting for internet archive job" in lowered:
        return CaptureOutcomeKind.DEFERRED_POLL_TIMEOUT

    if (
        "error:too-many-requests" in lowered
        or "overloaded" in lowered
        or "host is paused" in lowered
        or "crawling this host is paused" in lowered
    ):
        return CaptureOutcomeKind.DEFERRED_HOST_OVERLOAD

    if "error:user-session-limit" in lowered or "error:cannot-fetch" in lowered:
        return CaptureOutcomeKind.DEFERRED_IA_BUSY

    if lowered.startswith("error:") or ": error:" in lowered:
        return CaptureOutcomeKind.CAPTURE_ERROR

    return CaptureOutcomeKind.CAPTURE_ERROR


def outcome_kind_for_result(result: ArchiveResult) -> CaptureOutcomeKind:
    if result.status == "archived":
        return CaptureOutcomeKind.NEW_CAPTURE
    if result.status == "cached":
        return CaptureOutcomeKind.REUSED_SNAPSHOT
    if result.status == "skipped":
        return CaptureOutcomeKind.SKIPPED_ROBOTS
    return classify_archive_error(result.error)


def is_deferred_outcome(kind: CaptureOutcomeKind) -> bool:
    return kind in DEFERRED_OUTCOMES


def log_capture_outcome(
    log: logging.Logger,
    result: ArchiveResult,
    *,
    verbose: bool,
    cooldown_seconds: float | None = None,
) -> CaptureOutcomeKind:
    """Emit one log line that matches what happened; return the outcome kind."""
    kind = outcome_kind_for_result(result)
    url = result.url

    if kind == CaptureOutcomeKind.NEW_CAPTURE:
        if verbose:
            log.info("New capture: %s -> %s", url, result.archive_url)
        return kind

    if kind == CaptureOutcomeKind.REUSED_SNAPSHOT:
        if verbose:
            log.info(
                "Reused recent IA snapshot (no new crawl): %s -> %s",
                url,
                result.archive_url,
            )
        return kind

    if kind == CaptureOutcomeKind.SKIPPED_ROBOTS:
        if verbose:
            log.info("Skipped (robots.txt disallows fetch): %s", url)
        return kind

    if not verbose and kind not in DEFERRED_OUTCOMES:
        return kind

    detail = (result.error or "").strip()

    if kind == CaptureOutcomeKind.DEFERRED_SUBMIT_RATE_LIMIT:
        if cooldown_seconds is not None:
            log.warning(
                "IA throttled capture submit (HTTP 429); re-queued, "
                "pausing new submits %.0fs: %s",
                cooldown_seconds,
                url,
            )
        else:
            log.warning(
                "IA throttled capture submit (HTTP 429); re-queued: %s",
                url,
            )
        return kind

    if kind == CaptureOutcomeKind.DEFERRED_POLL_TIMEOUT:
        log.warning(
            "Status poll timed out (job may still complete at IA); re-queued: %s",
            url,
        )
        return kind

    if kind == CaptureOutcomeKind.DEFERRED_HOST_OVERLOAD:
        log.warning(
            "IA paused crawls for target host; re-queued: %s — %s",
            url,
            _short_detail(detail),
        )
        return kind

    if kind == CaptureOutcomeKind.DEFERRED_IA_BUSY:
        log.warning(
            "IA busy or session limit; re-queued: %s — %s",
            url,
            _short_detail(detail),
        )
        return kind

    if kind == CaptureOutcomeKind.BLOCKED_DAILY_URL_CAP:
        log.error(
            "IA URL daily cap reached (10 captures/URL/day); re-queued: %s",
            url,
        )
        return kind

    log.warning("IA capture error; re-queued: %s — %s", url, _short_detail(detail))
    return kind


def _short_detail(detail: str, max_len: int = 200) -> str:
    if len(detail) <= max_len:
        return detail
    return detail[: max_len - 3] + "..."

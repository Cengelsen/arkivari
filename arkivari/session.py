"""Quota-aware archive session driver."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from arkivari.archive import ArchiveRateLimiter, ArchiveResult, ArchiveSummary, archive_url
from arkivari.quota import wait_for_utc_quota_reset
from arkivari.queue import ArchiveQueue
from arkivari.robots import RobotsPolicy
from arkivari.spn_config import SpnConfig


def _is_rate_limited_error(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    if "error:too-many-daily-captures" in lowered:
        return False
    return "429" in error or "rate limit" in lowered


@dataclass
class SessionOutcome:
    summary: ArchiveSummary
    interrupted: bool = False
    segments: int = 1


def run_archive_session(
    queue: ArchiveQueue,
    queue_path: Path,
    robots: RobotsPolicy,
    user_agent: str,
    until_complete: bool,
    verbose: bool,
    log: logging.Logger,
    spn_config: SpnConfig,
    on_segment_complete: Callable[[ArchiveSummary, int], None] | None = None,
) -> SessionOutcome:
    """
    Archive pending URLs respecting UTC daily quota and per-minute rate limits.

    When until_complete is False, stops after using the remaining UTC quota for today.
    When until_complete is True, waits for UTC quota reset and continues until pending is empty.
    """
    summary = ArchiveSummary()
    rate_limiter = ArchiveRateLimiter(spn_config.min_interval_seconds)
    session = requests.Session()
    segment = 1
    session_budget: int | None = None if until_complete else queue.remaining_utc_quota()

    def finish_segment() -> None:
        nonlocal summary, segment
        if on_segment_complete and summary.results:
            on_segment_complete(summary, segment)
        summary = ArchiveSummary()
        segment += 1

    try:
        while queue.pending:
            if queue.remaining_utc_quota() == 0:
                if not until_complete:
                    break
                if on_segment_complete and summary.results:
                    finish_segment()
                if not wait_for_utc_quota_reset(log):
                    return SessionOutcome(summary=summary, interrupted=True, segments=segment)
                continue

            if session_budget is not None and session_budget <= 0:
                break

            url = queue.pending[0]
            if not robots.can_fetch(url):
                result = ArchiveResult(
                    url=url,
                    status="skipped",
                    error="Disallowed by robots.txt",
                )
                queue.apply_robots_skip(result)
                summary.add(result)
                queue.save(queue_path)
                if verbose:
                    log.info("Skipped (robots.txt): %s", url)
                continue

            while True:
                result = archive_url(
                    url,
                    user_agent,
                    rate_limiter,
                    session=session,
                    spn_config=spn_config,
                )
                if (
                    result.status == "failed"
                    and _is_rate_limited_error(result.error)
                    and until_complete
                ):
                    log.warning("Rate limited by Internet Archive; waiting for UTC quota reset")
                    if on_segment_complete and summary.results:
                        finish_segment()
                    if not wait_for_utc_quota_reset(log):
                        return SessionOutcome(summary=summary, interrupted=True, segments=segment)
                    continue

                break

            summary.add(result)
            if result.status in ("archived", "cached"):
                queue.apply_archive_result(result)
                if session_budget is not None:
                    session_budget -= 1
                queue.save(queue_path)
                if verbose:
                    if result.status == "archived":
                        log.info("Archived: %s -> %s", url, result.archive_url)
                    else:
                        log.info("Already cached: %s -> %s", url, result.archive_url)
            elif result.status == "failed":
                queue.pending.pop(0)
                queue.pending.append(url)
                queue.save(queue_path)
                if verbose:
                    log.error("Failed: %s (%s)", url, result.error)
            else:
                queue.apply_archive_result(result)
                queue.save(queue_path)

    except KeyboardInterrupt:
        queue.save(queue_path)
        log.warning("Archive interrupted; progress saved to %s", queue_path)
        return SessionOutcome(summary=summary, interrupted=True, segments=segment)

    if on_segment_complete and summary.results:
        on_segment_complete(summary, segment)

    return SessionOutcome(summary=summary, segments=segment)

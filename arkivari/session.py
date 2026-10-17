"""Quota-aware concurrent archive session driver."""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from arkivari.archive import ArchiveResult, ArchiveSummary, archive_url
from arkivari.quota import wait_for_utc_quota_reset
from arkivari.queue import ArchiveQueue
from arkivari.rate_limiter import CrawlRateLimiter, TokenBucketRateLimiter
from arkivari.robots import RobotsPolicy
from arkivari.spn_config import SpnConfig

SAVE_INTERVAL_SECONDS = 30.0
SAVE_BATCH_SIZE = 10
RATE_LIMIT_COOLDOWN_SECONDS = 30.0
# Minimum spacing between SPN /save/status polls across all worker threads.
SPN_POLL_MIN_INTERVAL_SECONDS = 2.0


def _is_poll_timeout_error(error: str | None) -> bool:
    if not error:
        return False
    return "timed out waiting for internet archive job" in error.lower()


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


def _capture_worker(
    url: str,
    user_agent: str,
    spn_config: SpnConfig,
    poll_limiter: CrawlRateLimiter,
) -> ArchiveResult:
    """Run a single capture in a worker thread (thread-local session)."""
    session = requests.Session()
    return archive_url(
        url,
        user_agent,
        session=session,
        spn_config=spn_config,
        poll_limiter=poll_limiter,
    )


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
    Archive pending URLs concurrently, respecting UTC daily quota and rate limits.

    Submits up to ``spn_config.max_concurrent`` capture jobs in parallel.
    Each submission is rate-limited by a shared :class:`TokenBucketRateLimiter`.
    The queue is saved periodically (every ~30 s or ~10 completions) instead of
    after every single URL, plus on interrupt and at session end.
    """
    summary = ArchiveSummary()
    rate_limiter = TokenBucketRateLimiter(spn_config.captures_per_minute)
    poll_limiter = CrawlRateLimiter(SPN_POLL_MIN_INTERVAL_SECONDS)
    segment = 1
    session_budget: int | None = None if until_complete else queue.remaining_utc_quota()

    last_save_at = time.monotonic()
    completions_since_save = 0

    def _needs_save() -> bool:
        return (
            completions_since_save >= SAVE_BATCH_SIZE
            or (time.monotonic() - last_save_at) >= SAVE_INTERVAL_SECONDS
        )

    def _do_save() -> None:
        nonlocal last_save_at, completions_since_save
        queue.save(queue_path)
        last_save_at = time.monotonic()
        completions_since_save = 0
        log.info(
            "Progress: %d archived, %d cached, %d failed, %d in-flight, %d pending",
            summary.archived_count,
            summary.cached_count,
            summary.failed_count,
            len(in_flight),
            queue.pending_count,
        )

    def finish_segment() -> None:
        nonlocal summary, segment
        if on_segment_complete and summary.results:
            on_segment_complete(summary, segment)
        summary = ArchiveSummary()
        segment += 1

    executor = ThreadPoolExecutor(max_workers=spn_config.max_concurrent)
    in_flight: dict[Future[ArchiveResult], str] = {}

    try:
        while queue.pending or in_flight:
            # -- quota gate --------------------------------------------------
            if queue.remaining_utc_quota() == 0 and not in_flight:
                if not until_complete:
                    break
                if on_segment_complete and summary.results:
                    finish_segment()
                if not wait_for_utc_quota_reset(log):
                    return SessionOutcome(summary=summary, interrupted=True, segments=segment)
                continue

            if session_budget is not None and session_budget <= 0 and not in_flight:
                break

            # -- fill slots ---------------------------------------------------
            while (
                len(in_flight) < spn_config.max_concurrent
                and queue.pending
                and (session_budget is None or session_budget > 0)
                and queue.remaining_utc_quota() > len(in_flight)
            ):
                url = queue.pop_next()
                if url is None:
                    break

                if not robots.can_fetch(url):
                    result = ArchiveResult(
                        url=url,
                        status="skipped",
                        error="Disallowed by robots.txt",
                    )
                    queue.apply_robots_skip(result)
                    summary.add(result)
                    if verbose:
                        log.info("Skipped (robots.txt): %s", url)
                    continue

                rate_limiter.acquire()
                future = executor.submit(
                    _capture_worker, url, user_agent, spn_config, poll_limiter,
                )
                in_flight[future] = url

            if not in_flight:
                continue

            # -- collect completions ------------------------------------------
            for future in as_completed(in_flight):
                url = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = ArchiveResult(url=url, status="failed", error=str(exc)[:300])

                summary.add(result)

                if result.status in ("archived", "cached"):
                    queue.apply_archive_result(result)
                    if session_budget is not None:
                        session_budget -= 1
                    if verbose:
                        if result.status == "archived":
                            log.info("Archived: %s -> %s", url, result.archive_url)
                        else:
                            log.info("Already cached: %s -> %s", url, result.archive_url)
                elif result.status == "failed":
                    if _is_rate_limited_error(result.error):
                        rate_limiter.cooldown(RATE_LIMIT_COOLDOWN_SECONDS)
                        log.warning(
                            "Rate limited on %s; cooling down %.0fs before next submission",
                            url,
                            RATE_LIMIT_COOLDOWN_SECONDS,
                        )
                        queue.move_to_back(url)
                    elif _is_poll_timeout_error(result.error):
                        log.warning(
                            "Poll timeout on %s; re-queuing (IA job may still complete)",
                            url,
                        )
                        queue.move_to_back(url)
                    else:
                        queue.move_to_back(url)
                    if verbose and not _is_poll_timeout_error(result.error):
                        log.error("Failed: %s (%s)", url, result.error)
                    elif verbose and _is_poll_timeout_error(result.error):
                        log.info("Re-queued after timeout: %s", url)
                else:
                    queue.apply_archive_result(result)

                completions_since_save += 1
                if _needs_save():
                    _do_save()

                break  # process one, then refill slots

    except KeyboardInterrupt:
        for f in in_flight:
            f.cancel()
        executor.shutdown(wait=False)
        queue.save(queue_path)
        log.warning("Archive interrupted; progress saved to %s", queue_path)
        return SessionOutcome(summary=summary, interrupted=True, segments=segment)

    executor.shutdown(wait=True)
    queue.save(queue_path)

    if on_segment_complete and summary.results:
        on_segment_complete(summary, segment)

    return SessionOutcome(summary=summary, segments=segment)

"""Quota-aware concurrent archive session driver."""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeAlias

import requests

from arkivari.archive import (
    ArchiveResult,
    ArchiveSummary,
    archive_url,
    check_spn_connectivity,
)
from arkivari.capture_outcome import (
    CaptureOutcomeKind,
    is_deferred_outcome,
    log_capture_outcome,
    outcome_kind_for_result,
)
from arkivari.quota import wait_for_utc_quota_reset
from arkivari.work_queue import ArchiveWorkQueue
from arkivari.rate_limiter import CrawlRateLimiter, TokenBucketRateLimiter
from arkivari.robots import RobotsPolicy
from arkivari.spn_config import SpnConfig

SAVE_INTERVAL_SECONDS = 30.0
SAVE_BATCH_SIZE = 10
RATE_LIMIT_COOLDOWN_SECONDS = 30.0
NETWORK_COOLDOWN_SECONDS = 120.0
# Minimum spacing between SPN /save/status polls across all worker threads.
SPN_POLL_MIN_INTERVAL_SECONDS = 2.0

CanFetchFn: TypeAlias = Callable[[str], bool]


@dataclass
class SessionOutcome:
    summary: ArchiveSummary
    interrupted: bool = False
    segments: int = 1


def _wait_for_spn_connectivity(
    log: logging.Logger,
    spn_config: SpnConfig,
    user_agent: str,
) -> bool:
    """Block until Save Page Now is reachable. Returns False if interrupted."""
    session = requests.Session()
    while True:
        ok, err = check_spn_connectivity(
            session,
            spn_config=spn_config,
            user_agent=user_agent,
        )
        if ok:
            return True
        detail = (err or "unknown error")[:200]
        log.warning(
            "Cannot reach Internet Archive Save Page Now (%s); retrying in %.0fs",
            detail,
            NETWORK_COOLDOWN_SECONDS,
        )
        try:
            time.sleep(NETWORK_COOLDOWN_SECONDS)
        except KeyboardInterrupt:
            log.info("Wait interrupted; queue saved — run again to resume")
            return False


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
    queue: ArchiveWorkQueue,
    queue_path: Path | None,
    robots: RobotsPolicy | None,
    user_agent: str,
    until_complete: bool,
    verbose: bool,
    log: logging.Logger,
    spn_config: SpnConfig,
    on_segment_complete: Callable[[ArchiveSummary, int], None] | None = None,
    can_fetch: CanFetchFn | None = None,
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

    def _url_allowed(url: str) -> bool:
        if can_fetch is not None:
            return can_fetch(url)
        if robots is None:
            return True
        return robots.can_fetch(url)

    def _persist() -> None:
        if queue_path is not None:
            queue.save(queue_path)

    def _needs_save() -> bool:
        return (
            completions_since_save >= SAVE_BATCH_SIZE
            or (time.monotonic() - last_save_at) >= SAVE_INTERVAL_SECONDS
        )

    def _do_save() -> None:
        nonlocal last_save_at, completions_since_save
        _persist()
        last_save_at = time.monotonic()
        completions_since_save = 0
        log.info(
            "Progress: %d new, %d reused snapshot, %d deferred retry, "
            "%d capture errors, %d in-flight, %d pending",
            summary.archived_count,
            summary.cached_count,
            summary.deferred_count,
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

    if queue.pending:
        if not _wait_for_spn_connectivity(log, spn_config, user_agent):
            executor.shutdown(wait=False)
            _persist()
            return SessionOutcome(summary=summary, interrupted=True, segments=segment)

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

                if not _url_allowed(url):
                    result = ArchiveResult(
                        url=url,
                        status="skipped",
                        error="Disallowed by robots.txt",
                    )
                    queue.apply_robots_skip(result)
                    summary.add(result)
                    log_capture_outcome(log, result, verbose=verbose)
                    continue

                rate_limiter.acquire()
                if verbose:
                    log.info(
                        "Submitting capture (%d/%d in-flight max): %s",
                        len(in_flight) + 1,
                        spn_config.max_concurrent,
                        url,
                    )
                future = executor.submit(
                    _capture_worker, url, user_agent, spn_config, poll_limiter,
                )
                in_flight[future] = url

            if not in_flight:
                continue

            # -- collect completions -----------------------------------------
            for future in as_completed(in_flight):
                url = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = ArchiveResult(url=url, status="failed", error=str(exc)[:300])

                kind = outcome_kind_for_result(result)
                deferred = result.status == "failed" and is_deferred_outcome(kind)
                summary.add(result, deferred=deferred)

                if result.status in ("archived", "cached"):
                    queue.apply_archive_result(result)
                    if session_budget is not None:
                        session_budget -= 1
                    log_capture_outcome(log, result, verbose=verbose)
                elif result.status == "failed":
                    cooldown: float | None = None
                    if kind == CaptureOutcomeKind.DEFERRED_SUBMIT_RATE_LIMIT:
                        rate_limiter.cooldown(RATE_LIMIT_COOLDOWN_SECONDS)
                        cooldown = RATE_LIMIT_COOLDOWN_SECONDS
                    elif kind == CaptureOutcomeKind.DEFERRED_NETWORK_ERROR:
                        rate_limiter.cooldown(NETWORK_COOLDOWN_SECONDS)
                        cooldown = NETWORK_COOLDOWN_SECONDS
                    queue.move_to_back(url)
                    log_capture_outcome(
                        log,
                        result,
                        verbose=True,
                        cooldown_seconds=cooldown,
                    )
                    if kind == CaptureOutcomeKind.DEFERRED_NETWORK_ERROR and not in_flight:
                        if not _wait_for_spn_connectivity(log, spn_config, user_agent):
                            for f in in_flight:
                                f.cancel()
                            executor.shutdown(wait=False)
                            _persist()
                            return SessionOutcome(
                                summary=summary,
                                interrupted=True,
                                segments=segment,
                            )
                else:
                    queue.apply_archive_result(result)
                    log_capture_outcome(log, result, verbose=verbose)

                completions_since_save += 1
                if _needs_save():
                    _do_save()

                break  # process one, then refill slots

    except KeyboardInterrupt:
        for f in in_flight:
            f.cancel()
        executor.shutdown(wait=False)
        _persist()
        dest = str(queue_path) if queue_path is not None else "database"
        log.warning(
            "Archive interrupted (%d in-flight, %d pending); progress saved to %s",
            len(in_flight),
            queue.pending_count,
            dest,
        )
        return SessionOutcome(summary=summary, interrupted=True, segments=segment)

    executor.shutdown(wait=True)
    _persist()

    if on_segment_complete and summary.results:
        on_segment_complete(summary, segment)

    return SessionOutcome(summary=summary, segments=segment)

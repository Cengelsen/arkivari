"""Tests for thread-safe rate limiters."""

from __future__ import annotations

import threading
import time

import pytest

from arkivari.rate_limiter import CrawlRateLimiter, TokenBucketRateLimiter


class TestTokenBucketRateLimiter:
    def test_first_acquire_immediate(self) -> None:
        limiter = TokenBucketRateLimiter(60)
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    def test_enforces_interval(self) -> None:
        limiter = TokenBucketRateLimiter(60)
        limiter.acquire()
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.9

    def test_capture_count(self) -> None:
        limiter = TokenBucketRateLimiter(600)
        assert limiter.capture_count == 0
        limiter.acquire()
        assert limiter.capture_count == 1
        limiter.acquire()
        assert limiter.capture_count == 2

    def test_cooldown_delays_next_acquire(self) -> None:
        limiter = TokenBucketRateLimiter(600)  # ~0.1s interval
        limiter.acquire()
        limiter.cooldown(1.0)
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.9

    def test_cooldown_shorter_than_interval_is_no_op(self) -> None:
        limiter = TokenBucketRateLimiter(1)  # 60s interval
        limiter.acquire()
        limiter.cooldown(0.5)  # shorter than the 60s interval
        # _last_token_at should not have changed backward
        assert limiter.capture_count == 1

    def test_concurrent_threads(self) -> None:
        limiter = TokenBucketRateLimiter(600)
        results: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            limiter.acquire()
            with lock:
                results.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(3)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 3
        assert limiter.capture_count == 3
        timestamps = sorted(results)
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            assert gap >= 0.09


class TestCrawlRateLimiter:
    def test_zero_delay_immediate(self) -> None:
        limiter = CrawlRateLimiter(0.0)
        start = time.monotonic()
        limiter.acquire()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.05

    def test_enforces_delay(self) -> None:
        limiter = CrawlRateLimiter(0.5)
        limiter.acquire()
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.45

    def test_thread_safety(self) -> None:
        limiter = CrawlRateLimiter(0.1)
        timestamps: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            limiter.acquire()
            with lock:
                timestamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(timestamps) == 3
        ordered = sorted(timestamps)
        for i in range(1, len(ordered)):
            gap = ordered[i] - ordered[i - 1]
            assert gap >= 0.09

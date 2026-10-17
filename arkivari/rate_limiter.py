"""Thread-safe rate limiters for crawling and IA submission."""

from __future__ import annotations

import threading
import time


class TokenBucketRateLimiter:
    """Thread-safe rate limiter for Internet Archive capture submissions.

    Enforces a maximum of *rate* submissions per minute across all threads.
    Calling :meth:`acquire` blocks until a token is available.
    """

    def __init__(self, rate_per_minute: int) -> None:
        self._min_interval = 60.0 / max(rate_per_minute, 1)
        self._lock = threading.Condition(threading.Lock())
        self._last_token_at = 0.0
        self._capture_count = 0

    def acquire(self) -> None:
        """Block until the rate limit allows a new submission."""
        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_token_at
                remaining = self._min_interval - elapsed
                if remaining <= 0:
                    self._last_token_at = now
                    self._capture_count += 1
                    return
                self._lock.wait(timeout=remaining)

    def cooldown(self, seconds: float) -> None:
        """Force the next token to be delayed by at least *seconds* from now.

        Called when a worker receives HTTP 429 so that all subsequent
        submissions are paused long enough for the rate limit to reset.
        """
        with self._lock:
            now = time.monotonic()
            needed = now - self._min_interval + seconds
            if needed > self._last_token_at:
                self._last_token_at = needed
            self._lock.notify_all()

    @property
    def capture_count(self) -> int:
        return self._capture_count


class CrawlRateLimiter:
    """Thread-safe interval limiter for crawl-delay enforcement.

    Ensures that at most one request is issued per *delay* seconds,
    regardless of how many threads are calling :meth:`acquire`.
    """

    def __init__(self, delay: float) -> None:
        self._delay = max(delay, 0.0)
        self._lock = threading.Condition(threading.Lock())
        self._last_request_at = 0.0

    def acquire(self) -> None:
        """Block until the crawl-delay interval has elapsed."""
        if self._delay <= 0:
            return
        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_request_at
                remaining = self._delay - elapsed
                if remaining <= 0:
                    self._last_request_at = now
                    return
                self._lock.wait(timeout=remaining)

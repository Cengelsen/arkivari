"""Tests for SPN status polling helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from arkivari.archive import POLL_429_BACKOFF_SECONDS, _retry_after_seconds


class TestRetryAfterSeconds:
    def test_missing_header_uses_default(self) -> None:
        response = MagicMock()
        response.headers = {}
        assert _retry_after_seconds(response, POLL_429_BACKOFF_SECONDS) == POLL_429_BACKOFF_SECONDS

    def test_parses_retry_after(self) -> None:
        response = MagicMock()
        response.headers = {"Retry-After": "45"}
        assert _retry_after_seconds(response, POLL_429_BACKOFF_SECONDS) == 45.0

    def test_caps_high_values(self) -> None:
        response = MagicMock()
        response.headers = {"Retry-After": "999"}
        assert _retry_after_seconds(response, POLL_429_BACKOFF_SECONDS) == 120.0

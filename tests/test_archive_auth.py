"""Tests for authenticated Save Page Now captures."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from arkivari.archive import ArchiveRateLimiter, archive_url
from arkivari.spn_config import SpnConfig


def _auth_config() -> SpnConfig:
    return SpnConfig(
        authenticated=True,
        access_key="access",
        secret_key="secret",
        captures_per_minute=7,
        daily_cap=100_000,
        min_interval_seconds=60.0 / 7,
    )


class TestArchiveRateLimiter:
    def test_authenticated_interval(self) -> None:
        limiter = ArchiveRateLimiter(60.0 / 7)
        assert limiter._min_interval_seconds == pytest.approx(8.571, rel=0.01)


class TestAuthenticatedArchiveUrl:
    @patch("arkivari.archive._poll_capture")
    @patch("arkivari.archive._submit_capture_authenticated")
    def test_success(
        self,
        mock_submit: MagicMock,
        mock_poll: MagicMock,
    ) -> None:
        mock_submit.return_value = ("job-123", None)
        mock_poll.return_value = {
            "status": "success",
            "timestamp": "20260101120000",
            "original_url": "https://example.com/",
        }
        config = _auth_config()
        limiter = ArchiveRateLimiter(config.min_interval_seconds)
        result = archive_url(
            "https://example.com/",
            "test-agent",
            limiter,
            spn_config=config,
        )
        assert result.status == "archived"
        assert result.archive_url is not None
        mock_submit.assert_called_once()

    @patch("arkivari.archive._poll_capture")
    @patch("arkivari.archive._submit_capture_authenticated")
    def test_job_error_includes_status_ext(
        self,
        mock_submit: MagicMock,
        mock_poll: MagicMock,
    ) -> None:
        mock_submit.return_value = ("job-123", None)
        mock_poll.return_value = {
            "status": "error",
            "status_ext": "error:no-access",
            "message": "Target URL could not be accessed",
        }
        config = _auth_config()
        limiter = ArchiveRateLimiter(config.min_interval_seconds)
        result = archive_url(
            "https://example.com/private",
            "test-agent",
            limiter,
            spn_config=config,
        )
        assert result.status == "failed"
        assert "error:no-access" in (result.error or "")

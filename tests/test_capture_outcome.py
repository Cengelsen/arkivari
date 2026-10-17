"""Tests for capture outcome classification and logging."""

from __future__ import annotations

import logging

import pytest

from arkivari.archive import ArchiveResult
from arkivari.capture_outcome import (
    CaptureOutcomeKind,
    classify_archive_error,
    is_deferred_outcome,
    log_capture_outcome,
    outcome_kind_for_result,
)


class TestClassifyArchiveError:
    def test_submit_429(self) -> None:
        assert (
            classify_archive_error("Rate limited by Internet Archive (HTTP 429)")
            == CaptureOutcomeKind.DEFERRED_SUBMIT_RATE_LIMIT
        )

    def test_poll_timeout(self) -> None:
        assert (
            classify_archive_error("Capture timed out waiting for Internet Archive job")
            == CaptureOutcomeKind.DEFERRED_POLL_TIMEOUT
        )

    def test_host_overload(self) -> None:
        msg = "please try again in ~1 min. crawling this host is paused because they are overloaded"
        assert classify_archive_error(msg) == CaptureOutcomeKind.DEFERRED_HOST_OVERLOAD

    def test_daily_cap(self) -> None:
        assert (
            classify_archive_error("error:too-many-daily-captures: limit")
            == CaptureOutcomeKind.BLOCKED_DAILY_URL_CAP
        )

    def test_ia_job_error(self) -> None:
        assert (
            classify_archive_error("error:no-access: Target URL could not be accessed")
            == CaptureOutcomeKind.CAPTURE_ERROR
        )


class TestDeferredOutcomes:
    def test_rate_limit_is_deferred(self) -> None:
        assert is_deferred_outcome(CaptureOutcomeKind.DEFERRED_SUBMIT_RATE_LIMIT)

    def test_capture_error_not_deferred(self) -> None:
        assert not is_deferred_outcome(CaptureOutcomeKind.CAPTURE_ERROR)


class TestOutcomeKindForResult:
    def test_archived(self) -> None:
        result = ArchiveResult(url="https://example.com/", status="archived", archive_url="https://ia/")
        assert outcome_kind_for_result(result) == CaptureOutcomeKind.NEW_CAPTURE


class TestLogCaptureOutcome:
    def test_deferred_logs_without_verbose(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        result = ArchiveResult(
            url="https://example.com/",
            status="failed",
            error="Rate limited by Internet Archive (HTTP 429)",
        )
        kind = log_capture_outcome(logging.getLogger("test"), result, verbose=False)
        assert kind == CaptureOutcomeKind.DEFERRED_SUBMIT_RATE_LIMIT
        assert any("HTTP 429" in r.message for r in caplog.records)

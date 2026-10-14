"""Tests for UTC quota reset timing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arkivari.quota import seconds_until_utc_quota_reset


class TestSecondsUntilUtcQuotaReset:
    def test_before_midnight_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fixed = datetime(2026, 10, 13, 23, 30, 0, tzinfo=timezone.utc)

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz else fixed.replace(tzinfo=None)

        monkeypatch.setattr("arkivari.quota.datetime", FakeDatetime)
        # 23:30 UTC -> 00:01:00 UTC next day (60s buffer after midnight)
        delay = seconds_until_utc_quota_reset(buffer_seconds=60)
        assert delay == 31 * 60

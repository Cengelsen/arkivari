"""Tests for archive queue UTC quota tracking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arkivari.archive import ArchiveResult
from arkivari.queue import QUEUE_VERSION, ArchiveQueue


class TestUtcQuota:
    def test_remaining_quota_empty_queue(self) -> None:
        queue = ArchiveQueue(domain="https://example.com", daily_limit=4000)
        assert queue.remaining_utc_quota() == 4000
        assert queue.today_batch_size() == 0

    def test_remaining_quota_with_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arkivari.queue.utc_today_iso", lambda: "2026-10-13")
        queue = ArchiveQueue(
            domain="https://example.com",
            pending=["https://example.com/a"],
            daily_limit=10,
            archives_by_date={"2026-10-13": 7},
        )
        assert queue.remaining_utc_quota() == 3
        assert queue.today_batch_size() == 1
        assert queue.archived_today_utc == 7

    def test_apply_archive_result_increments_quota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arkivari.queue.utc_today_iso", lambda: "2026-10-15")
        queue = ArchiveQueue(
            domain="https://example.com",
            pending=["https://example.com/page"],
            daily_limit=4000,
        )
        queue.apply_archive_result(
            ArchiveResult(
                url="https://example.com/page",
                status="archived",
                archive_url="https://web.archive.org/web/1/https://example.com/page",
            )
        )
        assert queue.pending == []
        assert queue.completed_count == 1
        assert queue.archives_by_date == {"2026-10-15": 1}

    def test_cached_counts_toward_quota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arkivari.queue.utc_today_iso", lambda: "2026-10-15")
        queue = ArchiveQueue(
            domain="https://example.com",
            pending=["https://example.com/page"],
        )
        queue.apply_archive_result(
            ArchiveResult(
                url="https://example.com/page",
                status="cached",
                archive_url="https://web.archive.org/web/1/https://example.com/page",
            )
        )
        assert queue.archives_by_date["2026-10-15"] == 1

    def test_failed_does_not_consume_quota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arkivari.queue.utc_today_iso", lambda: "2026-10-15")
        queue = ArchiveQueue(
            domain="https://example.com",
            pending=["https://example.com/page"],
        )
        queue.apply_archive_result(
            ArchiveResult(url="https://example.com/page", status="failed", error="nope")
        )
        assert queue.pending == ["https://example.com/page"]
        assert queue.archives_by_date == {}

    def test_robots_skip_no_quota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arkivari.queue.utc_today_iso", lambda: "2026-10-15")
        queue = ArchiveQueue(
            domain="https://example.com",
            pending=["https://example.com/blocked"],
        )
        queue.apply_robots_skip(
            ArchiveResult(
                url="https://example.com/blocked",
                status="skipped",
                error="Disallowed by robots.txt",
            )
        )
        assert queue.pending == []
        assert queue.archives_by_date == {}


class TestQueueMigration:
    def test_load_v1_legacy_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "queue.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "domain": "https://example.com",
                    "daily_limit": 100,
                    "last_archive_date": "2026-09-01",
                    "archived_today": 42,
                    "pending": [],
                    "completed": [],
                }
            ),
            encoding="utf-8",
        )
        queue = ArchiveQueue.load(path)
        assert queue.archives_by_date == {"2026-09-01": 42}

    def test_save_writes_v2(self, tmp_path: Path) -> None:
        path = tmp_path / "queue.json"
        queue = ArchiveQueue(
            domain="https://example.com",
            archives_by_date={"2026-10-13": 5},
        )
        queue.save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == QUEUE_VERSION
        assert data["archives_by_date"] == {"2026-10-13": 5}
        assert "last_archive_date" not in data

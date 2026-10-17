"""Persistent multi-day archive queue."""

from __future__ import annotations

import itertools
import json
import math
import re
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arkivari.archive import ANONYMOUS_DAILY_CAP, ArchiveResult
from arkivari.quota import utc_today_iso
from arkivari.robots import RobotsPolicy

QUEUE_VERSION = 2


@dataclass
class CompletedEntry:
    url: str
    status: str
    archive_url: str | None = None
    error: str | None = None
    archived_at: str | None = None


@dataclass
class ArchiveQueue:
    domain: str
    pending: deque[str] = field(default_factory=deque)
    completed: list[CompletedEntry] = field(default_factory=list)
    daily_limit: int = ANONYMOUS_DAILY_CAP
    created_at: str = ""
    updated_at: str = ""
    archives_by_date: dict[str, int] = field(default_factory=dict)
    discovery: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if not isinstance(self.pending, deque):
            self.pending = deque(self.pending)
        self._lock = threading.Lock()

    @property
    def completed_urls(self) -> set[str]:
        return {entry.url for entry in self.completed}

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    @property
    def completed_count(self) -> int:
        return len(self.completed)

    @property
    def total_count(self) -> int:
        return self.pending_count + self.completed_count

    @property
    def archived_today_utc(self) -> int:
        return self.quota_used_on(utc_today_iso())

    def days_remaining(self) -> int:
        if not self.pending:
            return 0
        return math.ceil(self.pending_count / self.daily_limit)

    def quota_used_on(self, iso_date: str) -> int:
        return self.archives_by_date.get(iso_date, 0)

    def remaining_utc_quota(self) -> int:
        today = utc_today_iso()
        return max(0, self.daily_limit - self.quota_used_on(today))

    def remaining_today_quota(self) -> int:
        """Alias for remaining UTC quota (Save Page Now uses UTC calendar days)."""
        return self.remaining_utc_quota()

    def today_batch_size(self) -> int:
        return min(self.pending_count, self.remaining_utc_quota())

    def is_complete(self) -> bool:
        return not self.pending

    def peek_batch(self, size: int) -> list[str]:
        return list(itertools.islice(self.pending, size))

    def pop_next(self) -> str | None:
        """Atomically pop the next pending URL (thread-safe)."""
        with self._lock:
            if not self.pending:
                return None
            return self.pending.popleft()

    def _bump_quota_for_today(self) -> None:
        today = utc_today_iso()
        self.archives_by_date[today] = self.quota_used_on(today) + 1

    def apply_robots_skip(self, result: ArchiveResult) -> None:
        """Remove a robots-disallowed URL from pending without using daily quota."""
        with self._lock:
            if self.pending and self.pending[0] == result.url:
                self.pending.popleft()
            now = datetime.now(timezone.utc).isoformat()
            self.completed.append(
                CompletedEntry(
                    url=result.url,
                    status=result.status,
                    archive_url=result.archive_url,
                    error=result.error,
                    archived_at=now,
                )
            )
            self.updated_at = now

    def apply_archive_result(self, result: ArchiveResult) -> None:
        """Record a completed archive result (thread-safe)."""
        with self._lock:
            if result.status == "failed":
                return

            now = datetime.now(timezone.utc).isoformat()
            try:
                self.pending.remove(result.url)
            except ValueError:
                pass
            self.completed.append(
                CompletedEntry(
                    url=result.url,
                    status=result.status,
                    archive_url=result.archive_url,
                    error=result.error,
                    archived_at=now,
                )
            )
            if result.status in ("archived", "cached"):
                self._bump_quota_for_today()
            self.updated_at = now

    def move_to_back(self, url: str) -> None:
        """Move a failed URL to the end of the pending queue (thread-safe)."""
        with self._lock:
            try:
                self.pending.remove(url)
            except ValueError:
                return
            self.pending.append(url)

    def save(self, path: Path) -> None:
        with self._lock:
            payload = {
                "version": QUEUE_VERSION,
                "domain": self.domain,
                "created_at": self.created_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "daily_limit": self.daily_limit,
                "archives_by_date": self.archives_by_date,
                "pending": list(self.pending),
                "completed": [asdict(entry) for entry in self.completed],
                "discovery": self.discovery,
            }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ArchiveQueue:
        data = json.loads(path.read_text(encoding="utf-8"))
        completed = [CompletedEntry(**entry) for entry in data.get("completed", [])]
        archives_by_date: dict[str, int] = dict(data.get("archives_by_date", {}))
        if not archives_by_date:
            legacy_date = data.get("last_archive_date", "")
            legacy_count = data.get("archived_today", 0)
            if legacy_date and legacy_count:
                archives_by_date[legacy_date] = legacy_count

        return cls(
            domain=data["domain"],
            pending=deque(data.get("pending", [])),
            completed=completed,
            daily_limit=data.get("daily_limit", ANONYMOUS_DAILY_CAP),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            archives_by_date=archives_by_date,
            discovery=data.get("discovery", {}),
        )

    @classmethod
    def from_urls(
        cls,
        domain: str,
        urls: list[str],
        robots: RobotsPolicy,
        daily_limit: int,
        discovery: dict[str, Any],
        existing: ArchiveQueue | None = None,
    ) -> ArchiveQueue:
        """Build a queue from discovered URLs, keeping prior completed work."""
        completed = list(existing.completed) if existing else []
        completed_urls = {entry.url for entry in completed}

        pending: list[str] = []
        skipped_robots = 0
        for url in urls:
            if url in completed_urls:
                continue
            if not robots.can_fetch(url):
                skipped_robots += 1
                continue
            pending.append(url)

        queue = cls(
            domain=domain,
            pending=deque(pending),
            completed=completed,
            daily_limit=daily_limit,
            created_at=existing.created_at if existing else "",
            archives_by_date=dict(existing.archives_by_date) if existing else {},
            discovery=discovery,
        )
        discovery["skipped_robots_at_queue"] = skipped_robots
        return queue


def default_queue_path(domain: str) -> Path:
    host = domain.lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return Path(f"arkivari-queue-{slug}.json")

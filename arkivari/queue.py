"""Persistent multi-day archive queue."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from arkivari.archive import ANONYMOUS_DAILY_CAP, ArchiveResult
from arkivari.robots import RobotsPolicy

QUEUE_VERSION = 1


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
    pending: list[str] = field(default_factory=list)
    completed: list[CompletedEntry] = field(default_factory=list)
    daily_limit: int = ANONYMOUS_DAILY_CAP
    created_at: str = ""
    updated_at: str = ""
    last_archive_date: str = ""
    archived_today: int = 0
    discovery: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

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

    def days_remaining(self) -> int:
        if not self.pending:
            return 0
        return math.ceil(self.pending_count / self.daily_limit)

    def remaining_today_quota(self) -> int:
        today = date.today().isoformat()
        if self.last_archive_date != today:
            return self.daily_limit
        return max(0, self.daily_limit - self.archived_today)

    def today_batch_size(self) -> int:
        return min(self.pending_count, self.remaining_today_quota())

    def is_complete(self) -> bool:
        return not self.pending

    def peek_batch(self, size: int) -> list[str]:
        return self.pending[:size]

    def finalize_batch(self, results: list[ArchiveResult], batch_size: int) -> None:
        """Apply archive results for the first batch_size pending URLs."""
        today = date.today().isoformat()
        if self.last_archive_date != today:
            self.last_archive_date = today
            self.archived_today = 0

        batch_urls = set(self.pending[:batch_size])
        results_by_url = {result.url: result for result in results}
        now = datetime.now(timezone.utc).isoformat()
        still_pending: list[str] = []

        for url in self.pending:
            if url not in batch_urls:
                still_pending.append(url)
                continue

            result = results_by_url.get(url)
            if result is None or result.status == "failed":
                still_pending.append(url)
                continue

            self.completed.append(
                CompletedEntry(
                    url=url,
                    status=result.status,
                    archive_url=result.archive_url,
                    error=result.error,
                    archived_at=now,
                )
            )
            if result.status in ("archived", "cached"):
                self.archived_today += 1

        self.pending = still_pending
        self.updated_at = now

    def save(self, path: Path) -> None:
        payload = {
            "version": QUEUE_VERSION,
            "domain": self.domain,
            "created_at": self.created_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "daily_limit": self.daily_limit,
            "last_archive_date": self.last_archive_date,
            "archived_today": self.archived_today,
            "pending": self.pending,
            "completed": [asdict(entry) for entry in self.completed],
            "discovery": self.discovery,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> ArchiveQueue:
        data = json.loads(path.read_text(encoding="utf-8"))
        completed = [CompletedEntry(**entry) for entry in data.get("completed", [])]
        return cls(
            domain=data["domain"],
            pending=list(data.get("pending", [])),
            completed=completed,
            daily_limit=data.get("daily_limit", ANONYMOUS_DAILY_CAP),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            last_archive_date=data.get("last_archive_date", ""),
            archived_today=data.get("archived_today", 0),
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
            pending=pending,
            completed=completed,
            daily_limit=daily_limit,
            created_at=existing.created_at if existing else "",
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

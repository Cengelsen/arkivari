"""Queue interface for archive sessions (JSON file or Postgres)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from arkivari.archive import ArchiveResult


class ArchiveWorkQueue(Protocol):
    """Operations required by :func:`arkivari.session.run_archive_session`."""

    @property
    def domain(self) -> str: ...

    @property
    def pending_count(self) -> int: ...

    @property
    def daily_limit(self) -> int: ...

    @property
    def pending(self) -> object:
        """Truthiness indicates whether work may remain (deque or flag for DB)."""

    def remaining_utc_quota(self) -> int: ...

    def pop_next(self) -> str | None: ...

    def apply_robots_skip(self, result: ArchiveResult) -> None: ...

    def apply_archive_result(self, result: ArchiveResult) -> None: ...

    def move_to_back(self, url: str) -> None: ...

    def save(self, path: Path) -> None: ...
"""Postgres work queue with SKIP LOCKED claims and UTC quota."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arkivari.archive import ANONYMOUS_DAILY_CAP, ArchiveResult
from arkivari.quota import utc_today_iso

logger = logging.getLogger(__name__)

# Link statuses the Go crawler may persist; archiver claims these for Save Page Now.
INCLUDABLE_LINK_STATUSES = frozenset(
    ("ok", "redirected", "forbidden", "auth_required")
)
LEASE_DURATION = timedelta(minutes=15)
INCLUDABLE_DB = tuple(sorted(INCLUDABLE_LINK_STATUSES))
NOTIFY_CHANNEL = "arkivari_pending"


@dataclass
class ClaimedUrl:
    url: str
    url_id: int
    domain_id: int
    base_url: str


class PostgresArchiveStore:
    """Multi-domain archive queue stored in PostgreSQL."""

    def __init__(
        self,
        pool: ConnectionPool,
        daily_limit: int,
        lease_owner: str | None = None,
    ) -> None:
        self._pool = pool
        self.daily_limit = daily_limit
        self._lease_owner = lease_owner or f"archiver-{uuid.uuid4().hex[:8]}"
        self._lock = threading.Lock()
        self._claimed: dict[str, ClaimedUrl] = {}
        self._has_pending: bool = True

    @classmethod
    def from_env(cls, daily_limit: int | None = None) -> PostgresArchiveStore:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is required")
        limit = daily_limit if daily_limit is not None else ANONYMOUS_DAILY_CAP
        pool = ConnectionPool(dsn, min_size=1, max_size=6, kwargs={"row_factory": dict_row})
        return cls(pool, limit)

    @property
    def domain(self) -> str:
        return "multi-domain"

    @property
    def pending(self) -> object:
        return self._has_pending or bool(self._claimed)

    @property
    def pending_count(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)::int AS c FROM urls u
                JOIN domains d ON d.id = u.domain_id
                WHERE u.archive_status IN ('pending', 'leased')
                  AND u.link_status = ANY(%s)
                  AND d.enabled = TRUE
                """,
                (list(INCLUDABLE_DB),),
            ).fetchone()
        return int(row["c"]) if row else 0

    def remaining_utc_quota(self) -> int:
        today = utc_today_iso()
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT success_count FROM quota_daily WHERE date_utc = %s",
                (today,),
            ).fetchone()
            used = int(row["success_count"]) if row else 0
        return max(0, self.daily_limit - used)

    def reconcile_stale_leases(self) -> int:
        with self._pool.connection() as conn:
            result = conn.execute(
                """
                UPDATE urls
                SET archive_status = 'pending',
                    lease_until = NULL,
                    lease_owner = NULL,
                    updated_at = NOW()
                WHERE archive_status = 'leased'
                  AND (lease_until IS NULL OR lease_until < NOW())
                """
            )
            conn.commit()
            return result.rowcount

    def refresh_pending_flag(self) -> None:
        self._has_pending = self.pending_count > 0

    def pop_next(self) -> str | None:
        with self._lock:
            with self._pool.connection() as conn:
                with conn.transaction():
                    row = conn.execute(
                        """
                        SELECT u.id, u.url, u.domain_id, d.base_url
                        FROM urls u
                        JOIN domains d ON d.id = u.domain_id
                        WHERE u.archive_status = 'pending'
                          AND u.link_status = ANY(%s)
                          AND d.enabled = TRUE
                        ORDER BY u.id
                        FOR UPDATE OF u SKIP LOCKED
                        LIMIT 1
                        """,
                        (list(INCLUDABLE_DB),),
                    ).fetchone()
                    if not row:
                        self._has_pending = False
                        return None
                    lease_until = datetime.now(timezone.utc) + LEASE_DURATION
                    conn.execute(
                        """
                        UPDATE urls
                        SET archive_status = 'leased',
                            lease_until = %s,
                            lease_owner = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (lease_until, self._lease_owner, row["id"]),
                    )
            claimed = ClaimedUrl(
                url=row["url"],
                url_id=int(row["id"]),
                domain_id=int(row["domain_id"]),
                base_url=row["base_url"],
            )
            self._claimed[claimed.url] = claimed
            return claimed.url

    def _map_archive_status(self, result: ArchiveResult) -> str:
        if result.status == "skipped":
            return "skipped_robots"
        if result.status in ("archived", "cached"):
            return result.status
        if result.status == "failed":
            return "failed"
        return "failed"

    def apply_robots_skip(self, result: ArchiveResult) -> None:
        with self._lock:
            claimed = self._claimed.pop(result.url, None)
            if not claimed:
                return
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    UPDATE urls
                    SET archive_status = 'skipped_robots',
                        last_error = %s,
                        lease_until = NULL,
                        lease_owner = NULL,
                        archived_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    ((result.error or "Disallowed by robots.txt")[:500], claimed.url_id),
                )
                conn.commit()

    def apply_archive_result(self, result: ArchiveResult) -> None:
        if result.status == "failed":
            return

        with self._lock:
            claimed = self._claimed.pop(result.url, None)
            if not claimed:
                return
            status = self._map_archive_status(result)
            with self._pool.connection() as conn:
                with conn.transaction():
                    conn.execute(
                        """
                        UPDATE urls
                        SET archive_status = %s::archive_status,
                            archive_url = %s,
                            last_error = %s,
                            archived_at = NOW(),
                            lease_until = NULL,
                            lease_owner = NULL,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            status,
                            result.archive_url,
                            (result.error[:500] if result.error else None),
                            claimed.url_id,
                        ),
                    )
                    if result.status in ("archived", "cached"):
                        conn.execute(
                            """
                            INSERT INTO quota_daily (date_utc, success_count)
                            VALUES (%s, 1)
                            ON CONFLICT (date_utc)
                            DO UPDATE SET success_count = quota_daily.success_count + 1
                            """,
                            (utc_today_iso(),),
                        )
            self._has_pending = True

    def move_to_back(self, url: str) -> None:
        with self._lock:
            claimed = self._claimed.pop(url, None)
            if not claimed:
                return
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    UPDATE urls
                    SET archive_status = 'pending',
                        attempt_count = attempt_count + 1,
                        last_error = COALESCE(last_error, 'deferred for retry'),
                        lease_until = NULL,
                        lease_owner = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (claimed.url_id,),
                )
                conn.commit()
            self._has_pending = True

    def release_failed_lease(self, url: str) -> None:
        """Return a failed leased URL to pending (no attempt bump)."""
        with self._lock:
            claimed = self._claimed.pop(url, None)
            if not claimed:
                return
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    UPDATE urls
                    SET archive_status = 'pending',
                        lease_until = NULL,
                        lease_owner = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (claimed.url_id,),
                )
                conn.commit()

    def save(self, path: Path) -> None:
        """No-op for Postgres; path ignored."""
        self.refresh_pending_flag()

    def close(self) -> None:
        self._pool.close()
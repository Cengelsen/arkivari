"""Import legacy JSON queue files into PostgreSQL."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

from arkivari.discover import normalize_domain, normalize_url
from arkivari.queue import ArchiveQueue


def _host_from_domain(domain: str) -> str:
    base = normalize_domain(domain)
    netloc = urlparse(base).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def import_queue_file(conn: psycopg.Connection, path: Path) -> tuple[int, int]:
    queue = ArchiveQueue.load(path)
    host = _host_from_domain(queue.domain)
    base_url = normalize_domain(queue.domain)
    now = datetime.now(timezone.utc)

    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO domains (host, base_url, enabled, max_pages, note, next_crawl_at)
            VALUES (%s, %s, TRUE, 0, %s, %s)
            ON CONFLICT (host) DO UPDATE SET
                base_url = EXCLUDED.base_url,
                updated_at = NOW()
            RETURNING id
            """,
            (host, base_url, f"imported from {path.name}", now),
        ).fetchone()
        domain_id = row["id"]

        imported = 0
        for url in queue.pending:
            norm = normalize_url(url)
            if not norm:
                continue
            conn.execute(
                """
                INSERT INTO urls (
                    domain_id, url, normalized_url, source, discovered_at,
                    link_status, archive_status
                )
                VALUES (%s, %s, %s, 'sitemap', %s, 'ok', 'pending')
                ON CONFLICT (normalized_url) DO UPDATE
                SET archive_status = 'pending'
                WHERE urls.archive_status NOT IN ('archived', 'cached')
                """,
                (domain_id, url, norm, now),
            )
            imported += 1

        archived = 0
        for entry in queue.completed:
            norm = normalize_url(entry.url)
            if not norm:
                continue
            status = entry.status
            if status == "cached":
                arch = "cached"
            elif status in ("archived", "skipped"):
                arch = "archived" if status == "archived" else "skipped_robots"
            else:
                arch = "failed"
            conn.execute(
                """
                INSERT INTO urls (
                    domain_id, url, normalized_url, source, discovered_at,
                    link_status, archive_status, archive_url, last_error, archived_at
                )
                VALUES (%s, %s, %s, 'sitemap', %s, 'ok', %s::archive_status, %s, %s, %s)
                ON CONFLICT (normalized_url) DO NOTHING
                """,
                (
                    domain_id,
                    entry.url,
                    norm,
                    now,
                    arch,
                    entry.archive_url,
                    entry.error,
                    entry.archived_at,
                ),
            )
            archived += 1

        for date_str, count in queue.archives_by_date.items():
            conn.execute(
                """
                INSERT INTO quota_daily (date_utc, success_count)
                VALUES (%s, %s)
                ON CONFLICT (date_utc) DO UPDATE SET
                    success_count = GREATEST(quota_daily.success_count, EXCLUDED.success_count)
                """,
                (date_str, count),
            )

        conn.execute("SELECT pg_notify('arkivari_pending', %s)", (str(domain_id),))

    return imported, archived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import arkivari JSON queue into Postgres")
    parser.add_argument("queue_files", nargs="+", type=Path)
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        for path in args.queue_files:
            pending, done = import_queue_file(conn, path)
            print(f"{path}: {pending} pending, {done} completed rows merged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
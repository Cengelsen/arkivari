"""PostgreSQL-backed archive queue for the daemon."""

from arkivari.db.store import PostgresArchiveStore

__all__ = ["PostgresArchiveStore"]
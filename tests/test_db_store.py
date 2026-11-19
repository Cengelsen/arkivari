"""Tests for Postgres archive store (integration when DATABASE_URL is set)."""

from __future__ import annotations

import os

import pytest

from arkivari.db.store import PostgresArchiveStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set",
)


@pytest.fixture
def store():
    st = PostgresArchiveStore.from_env(daily_limit=10)
    st.reconcile_stale_leases()
    yield st
    st.close()


def test_pending_count_empty(store: PostgresArchiveStore) -> None:
    assert store.pending_count >= 0
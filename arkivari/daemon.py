"""Long-running archiver: LISTEN/NOTIFY + Save Page Now."""

from __future__ import annotations

import logging
import os

import psycopg
import requests

from arkivari import DEFAULT_USER_AGENT
from arkivari.archive import verify_spn_credentials
from arkivari.db.robots_cache import RobotsCache
from arkivari.db.store import NOTIFY_CHANNEL, PostgresArchiveStore
from arkivari.session import run_archive_session
from arkivari.spn_config import load_dotenv, resolve_daily_limit, resolve_spn_config

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _wait_for_notify(conn: psycopg.Connection, timeout: float) -> bool:
    """Return True if a notification or timeout occurred (caller should claim work)."""
    if timeout <= 0:
        return True
    for _ in conn.notifies(timeout=timeout, stop_after=1):
        pass
    return True


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    user_agent = os.environ.get("ARKIVER_USER_AGENT", DEFAULT_USER_AGENT)
    idle_poll = _env_float("ARCHIVER_IDLE_POLL_SECONDS", 60.0)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL is required")
        return 2

    spn_config = resolve_spn_config(force_anonymous=os.environ.get("ARCHIVER_ANONYMOUS") == "1")
    daily_limit = resolve_daily_limit(None, spn_config, None)

    store = PostgresArchiveStore.from_env(daily_limit=daily_limit)
    store.reconcile_stale_leases()

    session = requests.Session()
    if spn_config.authenticated:
        ok, err, username = verify_spn_credentials(spn_config, user_agent, session)
        if not ok:
            logger.error("SPN credentials invalid: %s", err)
            return 2
        logger.info("Authenticated as %s", username)

    robots_cache = RobotsCache(user_agent)

    listen_conn = psycopg.connect(dsn, autocommit=True)
    listen_conn.execute(f"LISTEN {NOTIFY_CHANNEL}")

    log = logging.getLogger("arkivari.daemon")
    log.info(
        "Archiver daemon started (daily_limit=%d, idle_poll=%.0fs)",
        store.daily_limit,
        idle_poll,
    )

    try:
        while True:
            store.refresh_pending_flag()
            if store.pending_count == 0:
                _wait_for_notify(listen_conn, idle_poll)
                continue

            outcome = run_archive_session(
                store,
                queue_path=None,
                robots=None,
                user_agent=user_agent,
                until_complete=True,
                verbose=os.environ.get("ARCHIVER_VERBOSE") == "1",
                log=log,
                spn_config=spn_config,
                can_fetch=robots_cache.can_fetch,
            )
            if outcome.interrupted:
                return 130
    finally:
        listen_conn.close()
        store.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
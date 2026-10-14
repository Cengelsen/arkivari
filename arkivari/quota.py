"""UTC calendar-day quota timing for Save Page Now daily limits."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

UTC_QUOTA_RESET_BUFFER_SECONDS = 60


def utc_today_iso() -> str:
    """ISO date string for the current UTC calendar day."""
    return datetime.now(timezone.utc).date().isoformat()


def seconds_until_utc_quota_reset(buffer_seconds: int = UTC_QUOTA_RESET_BUFFER_SECONDS) -> float:
    """Seconds until the next UTC quota reset (00:00 UTC plus buffer)."""
    now = datetime.now(timezone.utc)
    next_day = now.date() + timedelta(days=1)
    reset_at = datetime(
        next_day.year,
        next_day.month,
        next_day.day,
        tzinfo=timezone.utc,
    ) + timedelta(seconds=buffer_seconds)
    return max(0.0, (reset_at - now).total_seconds())


def wait_for_utc_quota_reset(
    log: logging.Logger,
    buffer_seconds: int = UTC_QUOTA_RESET_BUFFER_SECONDS,
) -> bool:
    """Sleep until the next UTC quota reset. Returns False if interrupted."""
    delay = seconds_until_utc_quota_reset(buffer_seconds)
    if delay <= 0:
        return True

    resume_utc = datetime.now(timezone.utc) + timedelta(seconds=delay)
    log.info(
        "UTC daily quota reached; waiting until %s UTC (%s)",
        resume_utc.strftime("%Y-%m-%d %H:%M:%S"),
        _format_duration(delay),
    )

    try:
        end = time.monotonic() + delay
        while time.monotonic() < end:
            time.sleep(min(end - time.monotonic(), 300))
    except KeyboardInterrupt:
        log.info("Wait interrupted; queue saved — run again to resume")
        return False

    return True


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"~{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"~{minutes}m {secs}s" if secs else f"~{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"~{hours}h {minutes}m" if minutes else f"~{hours}h"

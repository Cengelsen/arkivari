"""Command-line interface for arkivari."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from arkivari import DEFAULT_USER_AGENT, __version__
from arkivari.archive import ANONYMOUS_DAILY_CAP, ArchiveSummary, estimate_archive_seconds
from arkivari.discover import DiscoveryStats, discover_urls, normalize_domain
from arkivari.quota import utc_today_iso
from arkivari.queue import ArchiveQueue, default_queue_path
from arkivari.session import run_archive_session
from arkivari.robots import RobotsPolicy
from arkivari.validate import ValidationStats, validate_urls


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arkivari",
        description=(
            "Discover public pages on a domain and archive them to Internet Archive "
            "on a multi-day schedule (4,000/day by default)."
        ),
    )
    parser.add_argument("domain", help="Domain to archive (e.g. example.com)")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum total pages to discover (0 = unlimited, default: 0)",
    )
    parser.add_argument(
        "--daily-limit",
        type=int,
        default=ANONYMOUS_DAILY_CAP,
        dest="daily_limit",
        help=f"Maximum URLs to archive per UTC calendar day (default: {ANONYMOUS_DAILY_CAP})",
    )
    parser.add_argument(
        "--max-archives",
        type=int,
        dest="daily_limit",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--queue-file",
        type=Path,
        default=None,
        help="Path for persistent archive queue (default: arkivari-queue-<domain>.json)",
    )
    parser.add_argument(
        "--rediscover",
        action="store_true",
        help="Re-run discovery and refresh the queue (keeps completed archives)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show queue status and exit without archiving",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover URLs and show schedule only; do not archive or update queue",
    )
    parser.add_argument(
        "--until-complete",
        action="store_true",
        help="Archive until the queue is finished; waits when daily UTC quota is used",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Archive without confirmation prompt",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("arkivari-results.json"),
        help="Path for JSON results report (default: arkivari-results.json)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent string for crawl and archive requests",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip link validation (do not check URLs for broken links before queueing)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable per-URL logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"~{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"~{minutes}m {secs}s" if secs else f"~{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"~{hours}h {minutes}m" if minutes else f"~{hours}h"


def _url_section(url: str) -> str:
    path = urlparse(url).path or "/"
    if path == "/":
        return "/ (homepage)"
    segment = path.strip("/").split("/")[0]
    return f"/{segment}/"


def _discovery_warnings(stats: DiscoveryStats, archivable_count: int) -> list[str]:
    warnings: list[str] = []
    if stats.discovery_capped:
        limit = stats.discovery_limit
        warnings.append(
            f"More than {limit:,} URLs exist on this site "
            f"(sitemap alone has {stats.sitemap_urls_found:,}); "
            f"increase --max-pages to discover more"
        )
    if stats.crawl_capped:
        warnings.append(
            "Crawl budget exhausted; additional pages may exist that are not in the sitemap"
        )
    if stats.sitemap_urls_found == 0 and stats.sitemap_failures:
        warnings.append(
            f"No sitemap URLs found ({len(stats.sitemap_failures)} sitemap source(s) failed); "
            "relying on link crawl only"
        )
    elif stats.sitemap_urls_found == 0:
        warnings.append("No sitemap found; relying on link crawl only")
    if archivable_count == 0:
        warnings.append("No archivable URLs found (all disallowed by robots.txt)")
    return warnings


def print_discovery_overview(
    domain: str,
    discovered_urls: list[str],
    archivable_count: int,
    stats: DiscoveryStats,
    daily_limit: int,
) -> None:
    """Print a summary of discovered URLs before queueing."""
    count = len(discovered_urls)
    days = (archivable_count + daily_limit - 1) // daily_limit if archivable_count else 0

    print(f"\nDiscovery complete for {domain}")
    print(f"  From sitemap:    {stats.sitemap_urls_selected:,}", end="")
    if stats.sitemap_urls_found > stats.sitemap_urls_selected:
        print(f" of {stats.sitemap_urls_found:,}", end="")
    print()
    if stats.crawl_skipped:
        print("  From crawl:      skipped (sitemap filled discovery limit)")
    else:
        print(f"  From crawl:      {stats.crawled_urls_found:,}")
    if stats.overlap:
        print(f"  Overlap:         {stats.overlap:,}")
    if stats.duplicates_skipped:
        print(f"  Duplicates:      {stats.duplicates_skipped:,} skipped")
    print(f"  Total discovered:{count:,}")
    print(f"  Archivable:      {archivable_count:,}")
    if archivable_count:
        print(f"  Schedule:        {days:,} day(s) at {daily_limit:,}/day")
        print(f"  Day 1 duration:  {_format_duration(estimate_archive_seconds(min(archivable_count, daily_limit)))}")

    for warning in _discovery_warnings(stats, archivable_count):
        print(f"  ! {warning}")

    sections = Counter(_url_section(url) for url in discovered_urls)
    if len(sections) > 1:
        print("  By section:")
        for section, section_count in sorted(sections.items(), key=lambda item: (-item[1], item[0])):
            print(f"    {section_count:>4}  {section}")
    print()


def print_validation_overview(stats: ValidationStats) -> None:
    """Print a summary of link validation results."""
    print(f"\nLink validation complete")
    print(f"  Checked:         {stats.total_checked:,}")
    print(f"  Included:        {stats.included:,}")
    if stats.ok:
        print(f"    OK (2xx):      {stats.ok:,}")
    if stats.redirected:
        print(f"    Redirected:    {stats.redirected:,}")
    if stats.forbidden:
        print(f"    Forbidden:     {stats.forbidden:,}")
    if stats.auth_required:
        print(f"    Auth required: {stats.auth_required:,}")
    if stats.excluded:
        print(f"  Excluded:        {stats.excluded:,}")
    if stats.not_found:
        print(f"    Not found:     {stats.not_found:,}")
    if stats.gone:
        print(f"    Gone (410):    {stats.gone:,}")
    if stats.soft_404:
        print(f"    Soft 404:      {stats.soft_404:,}")
    if stats.rate_limited:
        print(f"    Rate limited:  {stats.rate_limited:,}")
    if stats.server_error:
        print(f"    Server error:  {stats.server_error:,}")
    if stats.network_error:
        print(f"    Network error: {stats.network_error:,}")
    print()


def print_queue_overview(queue: ArchiveQueue, stats: DiscoveryStats | None = None) -> None:
    """Print the current archive schedule and progress."""
    print(f"\nArchive schedule for {queue.domain}")
    if stats:
        print(f"  Discovered:      {queue.total_count + stats.duplicates_skipped:,}", end="")
        skipped = queue.discovery.get("skipped_robots_at_queue", 0)
        if skipped:
            print(f" ({skipped:,} blocked by robots.txt)", end="")
        print()
    print(f"  Total queued:    {queue.total_count:,}")
    print(f"  Completed:       {queue.completed_count:,}")
    print(f"  Pending:         {queue.pending_count:,}")
    print(f"  Daily limit:     {queue.daily_limit:,} (UTC calendar day)")
    print(f"  UTC date:        {utc_today_iso()}")
    today_batch = queue.today_batch_size()
    print(f"  UTC today batch: {today_batch:,}")
    if queue.pending_count:
        print(f"  Days remaining:  {queue.days_remaining():,}")
    used_today = queue.archived_today_utc
    if used_today:
        remaining = queue.remaining_utc_quota()
        print(f"  Archived UTC:    {used_today:,} ({remaining:,} remaining today)")
    if today_batch:
        print(f"  Est. duration:   {_format_duration(estimate_archive_seconds(today_batch))}")
    if queue.is_complete():
        print("  Status:          complete")
    elif today_batch == 0 and queue.pending_count:
        print("  ! UTC daily quota exhausted; resumes after 00:00 UTC")
    print()


def confirm_archive(batch_size: int, until_complete: bool = False, pending: int = 0) -> bool:
    """Ask the user whether to proceed with archiving."""
    while True:
        if until_complete:
            days = (pending + batch_size - 1) // batch_size if batch_size else 0
            prompt = (
                f"Archive {pending:,} page(s) over ~{days:,} day(s) "
                f"({batch_size:,}/day) until complete? [y/N] "
            )
        else:
            prompt = f"Archive {batch_size:,} page(s) to Internet Archive? [y/N] "
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        print("Please answer yes or no.")


def _summary_to_report_dict(
    archive_summary: ArchiveSummary,
    queue: ArchiveQueue,
    queue_path: Path,
    segment: int,
    until_complete: bool,
) -> dict:
    return {
        "archived_count": archive_summary.archived_count,
        "cached_count": archive_summary.cached_count,
        "skipped_robots": archive_summary.skipped_robots,
        "failed_count": archive_summary.failed_count,
        "results": [
            {
                "url": result.url,
                "status": result.status,
                "archive_url": result.archive_url,
                "error": result.error,
            }
            for result in archive_summary.results
        ],
        "discovery": queue.discovery,
        "queue_file": str(queue_path),
        "segment": segment,
        "until_complete": until_complete,
        "quota_utc_date": utc_today_iso(),
        "archived_today_utc": queue.archived_today_utc,
    }


def write_report(
    output_path: Path,
    domain: str,
    queue: ArchiveQueue,
    summary: dict,
) -> None:
    report = {
        "domain": domain,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "queue_file": str(summary.get("queue_file", "")),
        "queue": {
            "total": queue.total_count,
            "completed": queue.completed_count,
            "pending": queue.pending_count,
            "daily_limit": queue.daily_limit,
            "days_remaining": queue.days_remaining(),
        },
        **summary,
    }
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def _count_archivable(urls: list[str], robots: RobotsPolicy) -> int:
    return sum(1 for url in urls if robots.can_fetch(url))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    log = logging.getLogger("arkivari")

    try:
        base_url = normalize_domain(args.domain)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    queue_path = args.queue_file or default_queue_path(base_url)
    existing_queue: ArchiveQueue | None = None
    if queue_path.exists():
        try:
            existing_queue = ArchiveQueue.load(queue_path)
        except (json.JSONDecodeError, KeyError) as exc:
            log.error("Failed to load queue file %s: %s", queue_path, exc)
            return 2

    session = requests.Session()
    robots = RobotsPolicy(base_url, args.user_agent, session=session)
    robots.load()

    if args.status:
        if existing_queue is None:
            log.error("No queue file found at %s", queue_path)
            return 2
        print_queue_overview(existing_queue)
        return 0

    discovery_stats: DiscoveryStats | None = None
    discovered_urls: list[str] = []
    queue: ArchiveQueue

    resume = (
        existing_queue is not None
        and not existing_queue.is_complete()
        and not args.rediscover
        and not args.dry_run
    )

    if resume:
        queue = existing_queue
        log.info("Resuming queue from %s (%d pending)", queue_path, queue.pending_count)
    else:
        try:
            discovery = discover_urls(
                args.domain,
                robots,
                session,
                args.user_agent,
                max_pages=args.max_pages,
                verbose=args.verbose,
            )
            discovered_urls = discovery.urls
            discovery_stats = discovery.stats
        except requests.RequestException as exc:
            log.error("Discovery failed: %s", exc)
            return 2

        if not discovered_urls:
            log.error("No URLs discovered for %s", base_url)
            return 2

        archivable = _count_archivable(discovered_urls, robots)
        print_discovery_overview(
            base_url,
            discovered_urls,
            archivable,
            discovery_stats,
            args.daily_limit,
        )

        validation_stats: ValidationStats | None = None
        if not args.skip_validation:
            print(f"Validating {len(discovered_urls):,} discovered links…")
            log.info("Validating %d discovered URLs…", len(discovered_urls))
            validation = validate_urls(
                discovered_urls,
                session,
                base_url,
                args.user_agent,
                robots,
                verbose=args.verbose,
            )
            validation_stats = validation.stats
            discovered_urls = validation.urls
            archivable = _count_archivable(discovered_urls, robots)
            print_validation_overview(validation_stats)

        if args.dry_run:
            log.info(
                "Dry run: %d URLs after validation (%d archivable)",
                len(discovered_urls),
                archivable,
            )
            return 0

        discovery_dict = asdict(discovery_stats)
        if validation_stats is not None:
            discovery_dict["validation"] = asdict(validation_stats)

        queue = ArchiveQueue.from_urls(
            base_url,
            discovered_urls,
            robots,
            args.daily_limit,
            discovery_dict,
            existing=existing_queue if args.rediscover else None,
        )
        queue.save(queue_path)
        log.info("Wrote queue with %d pending URLs to %s", queue.pending_count, queue_path)
        print_queue_overview(queue, discovery_stats)

    if resume:
        print_queue_overview(queue, discovery_stats)

    if queue.is_complete():
        log.info("Archive queue complete (%d URLs archived)", queue.completed_count)
        return 0

    if args.until_complete and args.dry_run:
        log.error("--until-complete cannot be used with --dry-run")
        return 2

    batch_size = queue.today_batch_size()
    if batch_size == 0 and not args.until_complete:
        log.info(
            "UTC daily quota (%d) already used for %s; run again after 00:00 UTC",
            args.daily_limit,
            utc_today_iso(),
        )
        return 0

    if not args.yes:
        confirm_size = queue.pending_count if args.until_complete else batch_size
        if not confirm_archive(
            args.daily_limit,
            until_complete=args.until_complete,
            pending=confirm_size,
        ):
            log.info("Archiving cancelled by user")
            return 0

    had_failures = False

    def on_segment_complete(archive_summary: ArchiveSummary, segment: int) -> None:
        nonlocal had_failures
        if archive_summary.failed_count > 0:
            had_failures = True
        summary = _summary_to_report_dict(
            archive_summary,
            queue,
            queue_path,
            segment,
            args.until_complete,
        )
        write_report(args.output, base_url, queue, summary)
        log.info(
            "Segment %d done: %d archived, %d cached, %d skipped (robots), %d failed",
            segment,
            archive_summary.archived_count,
            archive_summary.cached_count,
            archive_summary.skipped_robots,
            archive_summary.failed_count,
        )
        log.info(
            "Queue: %d completed, %d pending (%d day(s) remaining); UTC today %d/%d",
            queue.completed_count,
            queue.pending_count,
            queue.days_remaining(),
            queue.archived_today_utc,
            queue.daily_limit,
        )

    if args.until_complete:
        log.info(
            "Until complete: %d pending, up to %d per UTC day",
            queue.pending_count,
            queue.daily_limit,
        )
    else:
        log.info("Archiving up to %d URL(s) for UTC %s", batch_size, utc_today_iso())

    outcome = run_archive_session(
        queue,
        queue_path,
        robots,
        args.user_agent,
        until_complete=args.until_complete,
        verbose=args.verbose,
        log=log,
        on_segment_complete=on_segment_complete if args.until_complete else None,
    )

    if outcome.interrupted:
        return 130

    if not args.until_complete and outcome.summary.results:
        if outcome.summary.failed_count > 0:
            had_failures = True
        summary = _summary_to_report_dict(
            outcome.summary,
            queue,
            queue_path,
            1,
            args.until_complete,
        )
        write_report(args.output, base_url, queue, summary)
        log.info(
            "Batch done: %d archived, %d cached, %d skipped (robots), %d failed",
            outcome.summary.archived_count,
            outcome.summary.cached_count,
            outcome.summary.skipped_robots,
            outcome.summary.failed_count,
        )
        log.info(
            "Queue: %d completed, %d pending (%d day(s) remaining)",
            queue.completed_count,
            queue.pending_count,
            queue.days_remaining(),
        )

    if queue.is_complete():
        log.info("Archive queue complete (%d URLs archived)", queue.completed_count)
    log.info("Wrote report to %s", args.output)

    if had_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

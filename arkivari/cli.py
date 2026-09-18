"""Command-line interface for arkivari."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from arkivari import DEFAULT_USER_AGENT, __version__
from arkivari.archive import ANONYMOUS_DAILY_CAP, archive_urls, estimate_archive_seconds
from arkivari.discover import discover_urls, normalize_domain
from arkivari.robots import RobotsPolicy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arkivari",
        description="Discover public pages on a domain and archive them to Internet Archive.",
    )
    parser.add_argument("domain", help="Domain to archive (e.g. example.com)")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=500,
        help="Maximum pages to discover via crawling (default: 500)",
    )
    parser.add_argument(
        "--max-archives",
        type=int,
        default=ANONYMOUS_DAILY_CAP,
        help=f"Maximum URLs to submit to Internet Archive (default: {ANONYMOUS_DAILY_CAP})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover URLs only; do not archive",
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


def print_discovery_overview(
    domain: str,
    discovered_urls: list[str],
    max_archives: int,
    duplicates_skipped: int = 0,
) -> None:
    """Print a summary of discovered URLs before archiving."""
    count = len(discovered_urls)
    to_archive = min(count, max_archives)

    estimated = _format_duration(estimate_archive_seconds(to_archive))

    print(f"\nDiscovery complete for {domain}")
    print(f"  Pages found:     {count}")
    if duplicates_skipped:
        print(f"  Duplicates:      {duplicates_skipped} skipped")
    print(f"  To archive:      {to_archive}")
    print(f"  Est. duration:   {estimated}")

    sections = Counter(_url_section(url) for url in discovered_urls)
    if len(sections) > 1:
        print("  By section:")
        for section, section_count in sorted(sections.items(), key=lambda item: (-item[1], item[0])):
            print(f"    {section_count:>4}  {section}")
    print()


def confirm_archive() -> bool:
    """Ask the user whether to proceed with archiving."""
    while True:
        answer = input("Archive these pages to Internet Archive? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        print("Please answer yes or no.")


def write_report(
    output_path: Path,
    domain: str,
    discovered_urls: list[str],
    summary: dict,
) -> None:
    report = {
        "domain": domain,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "discovered_count": len(discovered_urls),
        **summary,
        "urls": discovered_urls,
    }
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


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

    session = requests.Session()
    robots = RobotsPolicy(base_url, args.user_agent, session=session)
    robots.load()

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
    except requests.RequestException as exc:
        log.error("Discovery failed: %s", exc)
        return 2

    if not discovered_urls:
        log.error("No URLs discovered for %s", base_url)
        return 2

    summary: dict = {
        "archived_count": 0,
        "cached_count": 0,
        "skipped_robots": 0,
        "failed_count": 0,
        "results": [],
        "dry_run": args.dry_run,
    }

    if args.dry_run:
        print_discovery_overview(
            base_url, discovered_urls, args.max_archives, discovery.duplicates_skipped
        )
        log.info("Dry run: discovered %d URLs", len(discovered_urls))
        for url in discovered_urls:
            summary["results"].append({"url": url, "status": "discovered"})
    else:
        print_discovery_overview(
            base_url, discovered_urls, args.max_archives, discovery.duplicates_skipped
        )
        if not args.yes and not confirm_archive():
            log.info("Archiving cancelled by user")
            for url in discovered_urls:
                summary["results"].append({"url": url, "status": "discovered"})
            summary["cancelled"] = True
            write_report(args.output, base_url, discovered_urls, summary)
            log.info("Wrote report to %s", args.output)
            return 0

        archive_summary = archive_urls(
            discovered_urls,
            robots,
            args.user_agent,
            max_archives=args.max_archives,
            verbose=args.verbose,
        )
        summary["archived_count"] = archive_summary.archived_count
        summary["cached_count"] = archive_summary.cached_count
        summary["skipped_robots"] = archive_summary.skipped_robots
        summary["failed_count"] = archive_summary.failed_count
        summary["results"] = [
            {
                "url": result.url,
                "status": result.status,
                "archive_url": result.archive_url,
                "error": result.error,
            }
            for result in archive_summary.results
        ]

        log.info(
            "Done: %d archived, %d cached, %d skipped (robots), %d failed",
            archive_summary.archived_count,
            archive_summary.cached_count,
            archive_summary.skipped_robots,
            archive_summary.failed_count,
        )

    write_report(args.output, base_url, discovered_urls, summary)
    log.info("Wrote report to %s", args.output)

    if args.dry_run:
        return 0
    if summary["failed_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

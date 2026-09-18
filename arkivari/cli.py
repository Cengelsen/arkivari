"""Command-line interface for arkivari."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from arkivari import DEFAULT_USER_AGENT, __version__
from arkivari.archive import ANONYMOUS_DAILY_CAP, archive_urls
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
        discovered_urls = discover_urls(
            args.domain,
            robots,
            session,
            args.user_agent,
            max_pages=args.max_pages,
            verbose=args.verbose,
        )
    except requests.RequestException as exc:
        log.error("Discovery failed: %s", exc)
        return 2

    if not discovered_urls:
        log.error("No URLs discovered for %s", base_url)
        return 2

    if len(discovered_urls) > args.max_archives and not args.dry_run:
        log.warning(
            "Discovered %d URLs but will archive at most %d this run",
            len(discovered_urls),
            args.max_archives,
        )

    summary: dict = {
        "archived_count": 0,
        "cached_count": 0,
        "skipped_robots": 0,
        "failed_count": 0,
        "results": [],
        "dry_run": args.dry_run,
    }

    if args.dry_run:
        log.info("Dry run: discovered %d URLs", len(discovered_urls))
        for url in discovered_urls:
            summary["results"].append({"url": url, "status": "discovered"})
    else:
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

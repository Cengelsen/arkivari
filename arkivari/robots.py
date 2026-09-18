"""robots.txt fetching and policy enforcement."""

from __future__ import annotations

import logging
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

logger = logging.getLogger(__name__)

DEFAULT_CRAWL_DELAY = 1.0


class RobotsPolicy:
    """Fetch and enforce robots.txt rules for a domain."""

    def __init__(self, base_url: str, user_agent: str, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self._parser = RobotFileParser()
        self._loaded = False
        self._last_request_at = 0.0
        self._crawl_delay = DEFAULT_CRAWL_DELAY

    def load(self) -> None:
        """Fetch robots.txt from the target domain."""
        parsed = urlparse(self.base_url)
        robots_urls = [
            f"https://{parsed.netloc}/robots.txt",
            f"http://{parsed.netloc}/robots.txt",
        ]

        for robots_url in robots_urls:
            try:
                response = self.session.get(
                    robots_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=15,
                )
            except requests.RequestException as exc:
                logger.warning("Failed to fetch %s: %s", robots_url, exc)
                continue

            if response.status_code == 200:
                self._parser.parse(response.text.splitlines())
                self._loaded = True
                self._crawl_delay = self._resolve_crawl_delay()
                logger.info("Loaded robots.txt from %s", robots_url)
                return

            if response.status_code == 404:
                logger.warning("No robots.txt at %s; treating as no restrictions", robots_url)
                self._apply_permissive_policy()
                return

        logger.warning("Could not load robots.txt; treating as no restrictions")
        self._apply_permissive_policy()

    def _apply_permissive_policy(self) -> None:
        self._parser.parse(["User-agent: *", "Allow: /"])
        self._loaded = True
        self._crawl_delay = DEFAULT_CRAWL_DELAY

    def _resolve_crawl_delay(self) -> float:
        delay = self._parser.crawl_delay(self.user_agent)
        if delay is None:
            delay = self._parser.crawl_delay("*")
        if delay is None:
            return DEFAULT_CRAWL_DELAY
        return max(float(delay), 0.0)

    def can_fetch(self, url: str) -> bool:
        if not self._loaded:
            self.load()
        return self._parser.can_fetch(self.user_agent, url)

    def site_maps(self) -> list[str]:
        if not self._loaded:
            self.load()
        sitemaps = self._parser.site_maps()
        if not sitemaps:
            return []
        return list(sitemaps)

    def wait_for_crawl(self) -> None:
        """Enforce crawl-delay between requests to the target site."""
        if self._crawl_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._crawl_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def default_sitemap_urls(self) -> list[str]:
        """Return sitemap URLs from robots.txt plus the conventional /sitemap.xml."""
        sitemaps = self.site_maps()
        conventional = urljoin(self.base_url + "/", "sitemap.xml")
        if conventional not in sitemaps:
            sitemaps.append(conventional)
        return sitemaps

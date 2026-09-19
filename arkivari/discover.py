"""URL discovery via sitemaps and bounded BFS crawling."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from arkivari.robots import RobotsPolicy

logger = logging.getLogger(__name__)

SKIP_SCHEMES = {"", "mailto", "javascript", "tel", "ftp", "data"}
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
INDEX_PATHS = {"/index.html", "/index.htm", "/index.php"}
SITEMAP_TIMEOUT_SECONDS = 60


@dataclass
class DiscoveryStats:
    discovery_limit: int
    sitemap_urls_found: int = 0
    sitemap_urls_selected: int = 0
    crawled_urls_found: int = 0
    overlap: int = 0
    duplicates_skipped: int = 0
    discovery_capped: bool = False
    crawl_capped: bool = False
    crawl_skipped: bool = False
    sitemap_failures: list[str] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    urls: list[str]
    stats: DiscoveryStats

    @property
    def duplicates_skipped(self) -> int:
        return self.stats.duplicates_skipped


def normalize_domain(domain: str) -> str:
    """Normalize a domain input to https://example.com form."""
    domain = domain.strip()
    if not domain:
        raise ValueError("Domain cannot be empty")

    if "://" not in domain:
        domain = f"https://{domain}"

    parsed = urlparse(domain)
    if not parsed.netloc:
        raise ValueError(f"Invalid domain: {domain}")

    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    return f"https://{netloc}"


def normalize_url(url: str) -> str | None:
    """Normalize a URL for deduplication; return None for non-http(s) links."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return None

    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = parsed.path or "/"
    if path.lower() in INDEX_PATHS:
        path = "/"
    elif path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse(("https", netloc, path, "", parsed.query, ""))


def deduplicate_urls(urls: list[str], verbose: bool = False) -> tuple[list[str], int]:
    """Remove duplicate URLs after normalization, keeping the first occurrence."""
    seen: set[str] = set()
    unique: list[str] = []
    duplicates_skipped = 0

    for url in urls:
        normalized = normalize_url(url)
        if not normalized:
            continue
        if normalized in seen:
            duplicates_skipped += 1
            if verbose:
                logger.info("Skipping duplicate URL: %s", url)
            continue
        seen.add(normalized)
        unique.append(normalized)

    return sorted(unique), duplicates_skipped


def same_domain(url: str, target_netloc: str) -> bool:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc == target_netloc


def is_html_response(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return any(content_type.startswith(t) for t in HTML_CONTENT_TYPES)


def parse_sitemap(
    sitemap_url: str,
    target_netloc: str,
    session: requests.Session,
    user_agent: str,
    robots: RobotsPolicy,
    failures: list[str],
    depth: int = 0,
    max_depth: int = 5,
) -> set[str]:
    """Parse a sitemap or sitemap index and return same-domain page URLs."""
    if depth > max_depth:
        logger.info("Sitemap index depth limit reached at %s", sitemap_url)
        return set()

    if not robots.can_fetch(sitemap_url):
        message = f"{sitemap_url}: disallowed by robots.txt"
        failures.append(message)
        logger.info("Skipping disallowed sitemap: %s", sitemap_url)
        return set()

    robots.wait_for_crawl()
    try:
        response = session.get(
            sitemap_url,
            headers={"User-Agent": user_agent},
            timeout=SITEMAP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        message = f"{sitemap_url}: {exc}"
        failures.append(message)
        logger.info("Failed to fetch sitemap %s: %s", sitemap_url, exc)
        return set()

    if response.status_code != 200:
        message = f"{sitemap_url}: HTTP {response.status_code}"
        failures.append(message)
        logger.info("Sitemap unavailable: %s (HTTP %d)", sitemap_url, response.status_code)
        return set()

    soup = BeautifulSoup(response.content, "xml")
    urls: set[str] = set()

    for sitemap_tag in soup.find_all("sitemap"):
        loc = sitemap_tag.find("loc")
        if loc and loc.text:
            nested = parse_sitemap(
                loc.text.strip(),
                target_netloc,
                session,
                user_agent,
                robots,
                failures,
                depth=depth + 1,
                max_depth=max_depth,
            )
            urls.update(nested)

    for url_tag in soup.find_all("url"):
        loc = url_tag.find("loc")
        if not loc or not loc.text:
            continue
        candidate = normalize_url(loc.text.strip())
        if candidate and same_domain(candidate, target_netloc):
            urls.add(candidate)

    if urls:
        logger.info("Sitemap %s: %d URLs", sitemap_url, len(urls))

    return urls


def discover_from_sitemaps(
    robots: RobotsPolicy,
    target_netloc: str,
    session: requests.Session,
    user_agent: str,
) -> tuple[set[str], list[str]]:
    urls: set[str] = set()
    failures: list[str] = []
    for sitemap_url in robots.default_sitemap_urls():
        found = parse_sitemap(
            sitemap_url,
            target_netloc,
            session,
            user_agent,
            robots,
            failures,
        )
        urls.update(found)
    return urls, failures


def _unlimited(max_pages: int) -> bool:
    return max_pages <= 0


def crawl_site(
    seed_url: str,
    target_netloc: str,
    robots: RobotsPolicy,
    session: requests.Session,
    user_agent: str,
    max_pages: int,
    exclude: set[str] | None = None,
    verbose: bool = False,
) -> tuple[set[str], bool]:
    """Bounded BFS crawl of HTML pages on the target domain.

    Returns discovered URLs (not in exclude) and whether the crawl hit its page budget
    while URLs remained in the queue. max_pages <= 0 means no limit.
    """
    exclude = exclude or set()
    discovered: set[str] = set()
    queue: deque[str] = deque([seed_url])
    seen: set[str] = set()

    while queue and (_unlimited(max_pages) or len(discovered) < max_pages):
        current = queue.popleft()
        normalized = normalize_url(current)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        if not same_domain(normalized, target_netloc):
            continue
        if not robots.can_fetch(normalized):
            if verbose:
                logger.info("Skipping disallowed URL: %s", normalized)
            continue

        robots.wait_for_crawl()
        try:
            response = session.get(normalized, headers={"User-Agent": user_agent}, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Failed to crawl %s: %s", normalized, exc)
            continue

        if response.status_code != 200:
            continue

        if not is_html_response(response):
            continue

        if normalized not in exclude:
            discovered.add(normalized)
            if verbose:
                logger.info("Crawled: %s", normalized)

        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"].strip()
            if not href or href.startswith("#"):
                continue

            parsed_href = urlparse(href)
            if parsed_href.scheme in SKIP_SCHEMES and parsed_href.scheme != "":
                continue

            full_url = normalize_url(urljoin(normalized, href))
            if not full_url:
                continue
            if not same_domain(full_url, target_netloc):
                continue
            if full_url not in seen:
                queue.append(full_url)

    crawl_capped = bool(queue) and not _unlimited(max_pages) and len(discovered) >= max_pages
    return discovered, crawl_capped


def discover_urls(
    domain: str,
    robots: RobotsPolicy,
    session: requests.Session,
    user_agent: str,
    max_pages: int,
    verbose: bool = False,
) -> DiscoveryResult:
    """Discover public URLs on a domain via sitemaps (first) and crawling."""
    base_url = normalize_domain(domain)
    target_netloc = urlparse(base_url).netloc.lower()
    if target_netloc.startswith("www."):
        target_netloc = target_netloc[4:]

    seed_url = base_url + "/"
    stats = DiscoveryStats(discovery_limit=max_pages)

    sitemap_urls, stats.sitemap_failures = discover_from_sitemaps(
        robots, target_netloc, session, user_agent
    )
    stats.sitemap_urls_found = len(sitemap_urls)

    if _unlimited(max_pages):
        sitemap_selected = sorted(sitemap_urls)
        stats.sitemap_urls_selected = len(sitemap_selected)
        stats.discovery_capped = False
        crawl_budget = -1
    else:
        sitemap_selected = sorted(sitemap_urls)[:max_pages]
        stats.sitemap_urls_selected = len(sitemap_selected)
        stats.discovery_capped = stats.sitemap_urls_found > max_pages
        crawl_budget = max_pages - len(sitemap_selected)

    if crawl_budget == 0:
        stats.crawl_skipped = True
        crawled_urls: set[str] = set()
        stats.crawl_capped = False
    else:
        stats.crawl_skipped = False
        crawled_urls, stats.crawl_capped = crawl_site(
            seed_url,
            target_netloc,
            robots,
            session,
            user_agent,
            max_pages=crawl_budget,
            exclude=set(sitemap_selected),
            verbose=verbose,
        )

    stats.crawled_urls_found = len(crawled_urls)
    stats.overlap = len(set(sitemap_selected) & crawled_urls)

    combined = list(sitemap_selected) + list(crawled_urls)
    all_urls, stats.duplicates_skipped = deduplicate_urls(combined, verbose=verbose)

    if not _unlimited(max_pages) and len(all_urls) > max_pages:
        all_urls = all_urls[:max_pages]
        stats.discovery_capped = True

    logger.info(
        "Discovered %d URLs (sitemap: %d, crawl: %d, overlap: %d, duplicates: %d)",
        len(all_urls),
        stats.sitemap_urls_selected,
        stats.crawled_urls_found,
        stats.overlap,
        stats.duplicates_skipped,
    )

    return DiscoveryResult(urls=all_urls, stats=stats)

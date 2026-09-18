"""URL discovery via sitemaps and bounded BFS crawling."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from arkivari.robots import RobotsPolicy

logger = logging.getLogger(__name__)

SKIP_SCHEMES = {"", "mailto", "javascript", "tel", "ftp", "data"}
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
INDEX_PATHS = {"/index.html", "/index.htm", "/index.php"}


@dataclass
class DiscoveryResult:
    urls: list[str]
    duplicates_skipped: int


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
    depth: int = 0,
    max_depth: int = 3,
) -> set[str]:
    """Parse a sitemap or sitemap index and return same-domain page URLs."""
    if depth > max_depth:
        return set()

    if not robots.can_fetch(sitemap_url):
        logger.debug("Skipping disallowed sitemap: %s", sitemap_url)
        return set()

    robots.wait_for_crawl()
    try:
        response = session.get(sitemap_url, headers={"User-Agent": user_agent}, timeout=15)
    except requests.RequestException as exc:
        logger.warning("Failed to fetch sitemap %s: %s", sitemap_url, exc)
        return set()

    if response.status_code != 200:
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

    return urls


def discover_from_sitemaps(
    robots: RobotsPolicy,
    target_netloc: str,
    session: requests.Session,
    user_agent: str,
) -> set[str]:
    urls: set[str] = set()
    for sitemap_url in robots.default_sitemap_urls():
        found = parse_sitemap(sitemap_url, target_netloc, session, user_agent, robots)
        urls.update(found)
    return urls


def crawl_site(
    seed_url: str,
    target_netloc: str,
    robots: RobotsPolicy,
    session: requests.Session,
    user_agent: str,
    max_pages: int,
    verbose: bool = False,
) -> set[str]:
    """Bounded BFS crawl of HTML pages on the target domain."""
    discovered: set[str] = set()
    queue: deque[str] = deque([seed_url])

    while queue and len(discovered) < max_pages:
        current = queue.popleft()
        normalized = normalize_url(current)
        if not normalized or normalized in discovered:
            continue
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
            if full_url not in discovered:
                queue.append(full_url)

    return discovered


def discover_urls(
    domain: str,
    robots: RobotsPolicy,
    session: requests.Session,
    user_agent: str,
    max_pages: int,
    verbose: bool = False,
) -> DiscoveryResult:
    """Discover public URLs on a domain via sitemaps and crawling."""
    base_url = normalize_domain(domain)
    target_netloc = urlparse(base_url).netloc.lower()
    if target_netloc.startswith("www."):
        target_netloc = target_netloc[4:]

    seed_url = base_url + "/"
    sitemap_urls = discover_from_sitemaps(robots, target_netloc, session, user_agent)
    crawled_urls = crawl_site(
        seed_url,
        target_netloc,
        robots,
        session,
        user_agent,
        max_pages=max_pages,
        verbose=verbose,
    )

    combined = list(sitemap_urls) + list(crawled_urls)
    all_urls, duplicates_skipped = deduplicate_urls(combined, verbose=verbose)
    logger.info(
        "Discovered %d unique URLs (%d duplicates skipped)",
        len(all_urls),
        duplicates_skipped,
    )
    return DiscoveryResult(urls=all_urls, duplicates_skipped=duplicates_skipped)

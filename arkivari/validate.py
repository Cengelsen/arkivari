"""Link validation: check discovered URLs and filter out broken links."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

import requests

from arkivari.robots import RobotsPolicy

logger = logging.getLogger(__name__)

LinkStatus = Literal[
    "ok",
    "redirected",
    "not_found",
    "gone",
    "forbidden",
    "auth_required",
    "rate_limited",
    "server_error",
    "network_error",
    "soft_404",
]

INCLUDABLE_STATUSES: frozenset[LinkStatus] = frozenset(
    ("ok", "redirected", "forbidden", "auth_required")
)
TRANSIENT_STATUS_CODES = (429, 502, 503)
MAX_RETRIES = 3
VALIDATION_TIMEOUT_SECONDS = 20
HEAD_INCONCLUSIVE_CODES = (405, 501)


@dataclass
class LinkCheckResult:
    url: str
    status: LinkStatus
    http_code: int = 0
    final_url: str | None = None
    error: str | None = None


@dataclass
class ValidationStats:
    total_checked: int = 0
    ok: int = 0
    redirected: int = 0
    not_found: int = 0
    gone: int = 0
    soft_404: int = 0
    forbidden: int = 0
    auth_required: int = 0
    rate_limited: int = 0
    server_error: int = 0
    network_error: int = 0

    def record(self, status: LinkStatus) -> None:
        self.total_checked += 1
        current = getattr(self, status, 0)
        setattr(self, status, current + 1)

    @property
    def included(self) -> int:
        return self.ok + self.redirected + self.forbidden + self.auth_required

    @property
    def excluded(self) -> int:
        return self.total_checked - self.included


@dataclass
class ValidationResult:
    urls: list[str]
    stats: ValidationStats
    results: list[LinkCheckResult] = field(default_factory=list)


def _classify_status_code(code: int) -> LinkStatus:
    """Map an HTTP status code to a LinkStatus."""
    if 200 <= code <= 299:
        return "ok"
    if code == 401:
        return "auth_required"
    if code == 403:
        return "forbidden"
    if code == 404:
        return "not_found"
    if code == 410:
        return "gone"
    if code == 429:
        return "rate_limited"
    if 500 <= code <= 599:
        return "server_error"
    return "server_error"


def _normalize_final_url(url: str) -> str:
    """Normalize a URL for comparison (strip fragment, lowercase netloc)."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


def probe_error_page(
    base_url: str,
    session: requests.Session,
    user_agent: str,
) -> str | None:
    """Request a known-bad path to discover the site's canonical 404 destination.

    Returns the normalized final URL after redirects, or None if the probe
    itself fails (network error, non-redirect 404, etc.).
    """
    token = secrets.token_hex(8)
    probe_url = f"{base_url.rstrip('/')}/arkivari-probe-404-{token}"

    try:
        response = session.get(
            probe_url,
            headers={"User-Agent": user_agent},
            timeout=VALIDATION_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        logger.info("Error-page probe failed: %s", exc)
        return None

    if response.is_redirect or 300 <= response.status_code < 400:
        return None

    if response.status_code == 404:
        return None

    if 200 <= response.status_code <= 299 and response.url != probe_url:
        final = _normalize_final_url(response.url)
        logger.info("Detected soft-404 destination: %s", final)
        return final

    return None


def _do_request(
    method: str,
    url: str,
    session: requests.Session,
    user_agent: str,
) -> requests.Response:
    """Issue a single HTTP request with standard headers."""
    return session.request(
        method,
        url,
        headers={"User-Agent": user_agent},
        timeout=VALIDATION_TIMEOUT_SECONDS,
        allow_redirects=True,
    )


def check_link(
    url: str,
    session: requests.Session,
    user_agent: str,
    error_page_url: str | None = None,
) -> LinkCheckResult:
    """Check a single URL with HEAD-then-GET and classify the response."""
    response: requests.Response | None = None
    last_error: str | None = None
    backoff = 2.0

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = _do_request("HEAD", url, session, user_agent)

            if response.status_code in HEAD_INCONCLUSIVE_CODES:
                response = _do_request("GET", url, session, user_agent)

            code = response.status_code

            if code in TRANSIENT_STATUS_CODES:
                last_error = f"HTTP {code} on attempt {attempt}/{MAX_RETRIES}"
                if attempt < MAX_RETRIES:
                    logger.info(
                        "Transient %d for %s (attempt %d/%d); retrying in %.1fs",
                        code, url, attempt, MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                return LinkCheckResult(
                    url=url,
                    status=_classify_status_code(code),
                    http_code=code,
                    final_url=response.url,
                    error=last_error,
                )

            break

        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                logger.info(
                    "Network error for %s (attempt %d/%d): %s; retrying in %.1fs",
                    url, attempt, MAX_RETRIES, exc, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            return LinkCheckResult(
                url=url,
                status="network_error",
                http_code=0,
                error=last_error,
            )

    if response is None:
        return LinkCheckResult(
            url=url,
            status="network_error",
            http_code=0,
            error=last_error or "No response received",
        )

    code = response.status_code
    final_url = response.url
    was_redirected = final_url != url

    status = _classify_status_code(code)
    if status == "ok" and was_redirected:
        if (
            error_page_url
            and _normalize_final_url(final_url) == error_page_url
        ):
            return LinkCheckResult(
                url=url,
                status="soft_404",
                http_code=code,
                final_url=final_url,
                error="Redirected to site error page",
            )
        status = "redirected"

    return LinkCheckResult(
        url=url,
        status=status,
        http_code=code,
        final_url=final_url if was_redirected else None,
        error=last_error,
    )


def validate_urls(
    urls: list[str],
    session: requests.Session,
    base_url: str,
    user_agent: str,
    robots: RobotsPolicy,
    verbose: bool = False,
) -> ValidationResult:
    """Check all discovered URLs and return only those with valid responses."""
    stats = ValidationStats()
    results: list[LinkCheckResult] = []
    accepted: list[str] = []

    error_page_url = probe_error_page(base_url, session, user_agent)

    total = len(urls)
    for i, url in enumerate(urls, 1):
        robots.wait_for_crawl()

        result = check_link(url, session, user_agent, error_page_url)
        stats.record(result.status)
        results.append(result)

        if result.status in INCLUDABLE_STATUSES:
            accepted.append(url)

        if verbose:
            code_str = str(result.http_code) if result.http_code else "---"
            extra = ""
            if result.final_url:
                extra = f" -> {result.final_url}"
            if result.error:
                extra += f" ({result.error})"
            logger.info(
                "[%d/%d] %s %s %s%s",
                i, total, code_str, result.status, url, extra,
            )

    logger.info(
        "Validation complete: %d checked, %d included, %d excluded",
        stats.total_checked,
        stats.included,
        stats.excluded,
    )

    return ValidationResult(urls=accepted, stats=stats, results=results)

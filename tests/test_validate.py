"""Tests for arkivari.validate — link validation and filtering."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from arkivari.validate import (
    INCLUDABLE_STATUSES,
    LinkCheckResult,
    ValidationStats,
    _classify_status_code,
    _normalize_final_url,
    check_link,
    probe_error_page,
    validate_urls,
)


# ---------------------------------------------------------------------------
# _classify_status_code
# ---------------------------------------------------------------------------

class TestClassifyStatusCode:
    @pytest.mark.parametrize("code", [200, 201, 204, 299])
    def test_2xx_is_ok(self, code: int):
        assert _classify_status_code(code) == "ok"

    def test_401(self):
        assert _classify_status_code(401) == "auth_required"

    def test_403(self):
        assert _classify_status_code(403) == "forbidden"

    def test_404(self):
        assert _classify_status_code(404) == "not_found"

    def test_410(self):
        assert _classify_status_code(410) == "gone"

    def test_429(self):
        assert _classify_status_code(429) == "rate_limited"

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 599])
    def test_5xx_is_server_error(self, code: int):
        assert _classify_status_code(code) == "server_error"


# ---------------------------------------------------------------------------
# _normalize_final_url
# ---------------------------------------------------------------------------

class TestNormalizeFinalUrl:
    def test_strips_www(self):
        assert _normalize_final_url("https://www.example.com/page") == "https://example.com/page"

    def test_strips_trailing_slash(self):
        assert _normalize_final_url("https://example.com/page/") == "https://example.com/page"

    def test_keeps_root_slash(self):
        assert _normalize_final_url("https://example.com/") == "https://example.com/"

    def test_lowercases_netloc(self):
        assert _normalize_final_url("https://EXAMPLE.COM/Page") == "https://example.com/Page"


# ---------------------------------------------------------------------------
# ValidationStats
# ---------------------------------------------------------------------------

class TestValidationStats:
    def test_record_increments(self):
        stats = ValidationStats()
        stats.record("ok")
        stats.record("ok")
        stats.record("not_found")
        assert stats.total_checked == 3
        assert stats.ok == 2
        assert stats.not_found == 1

    def test_included_excluded(self):
        stats = ValidationStats()
        stats.record("ok")
        stats.record("redirected")
        stats.record("not_found")
        stats.record("gone")
        assert stats.included == 2
        assert stats.excluded == 2


# ---------------------------------------------------------------------------
# probe_error_page
# ---------------------------------------------------------------------------

class TestProbeErrorPage:
    def test_returns_none_on_404(self):
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 404
        resp.is_redirect = False
        session.get.return_value = resp
        assert probe_error_page("https://example.com", session, "test-agent") is None

    def test_returns_final_url_on_soft_404(self):
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.is_redirect = False
        resp.url = "https://example.com/404-page"
        session.get.return_value = resp
        result = probe_error_page("https://example.com", session, "test-agent")
        assert result == "https://example.com/404-page"

    def test_returns_none_on_network_error(self):
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("DNS failure")
        assert probe_error_page("https://example.com", session, "test-agent") is None

    def test_returns_none_when_no_redirect(self):
        """If the probe URL itself returns 200 without redirecting, no soft-404 detected."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.is_redirect = False
        # URL matches the probe URL (no redirect happened)
        session.get.return_value = resp
        # The probe URL includes a random token so it won't match exactly,
        # but we can simulate by setting resp.url to the same thing passed in
        resp.url = "https://example.com/arkivari-probe-404-abc123"
        # Since the mock get() was called with a different URL (the real probe),
        # the check is url != probe_url — the mock url matches the call arg
        # Let's just set it to match:
        def side_effect(url, **kwargs):
            resp.url = url
            return resp
        session.get.side_effect = side_effect
        assert probe_error_page("https://example.com", session, "test-agent") is None


# ---------------------------------------------------------------------------
# check_link
# ---------------------------------------------------------------------------

def _mock_response(status_code: int, url: str) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.url = url
    return resp


class TestCheckLink:
    def test_ok_no_redirect(self):
        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(200, "https://example.com/page")

        result = check_link("https://example.com/page", session, "test-agent")
        assert result.status == "ok"
        assert result.http_code == 200
        assert result.final_url is None

    def test_redirect_detected(self):
        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(200, "https://example.com/new-page")

        result = check_link("https://example.com/old-page", session, "test-agent")
        assert result.status == "redirected"
        assert result.http_code == 200
        assert result.final_url == "https://example.com/new-page"

    def test_404_classified(self):
        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(404, "https://example.com/missing")

        result = check_link("https://example.com/missing", session, "test-agent")
        assert result.status == "not_found"
        assert result.http_code == 404

    def test_410_classified(self):
        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(410, "https://example.com/removed")

        result = check_link("https://example.com/removed", session, "test-agent")
        assert result.status == "gone"
        assert result.http_code == 410

    def test_head_fallback_to_get_on_405(self):
        """When HEAD returns 405, should retry with GET."""
        session = MagicMock(spec=requests.Session)

        head_resp = _mock_response(405, "https://example.com/page")
        get_resp = _mock_response(200, "https://example.com/page")

        session.request.side_effect = [head_resp, get_resp]

        result = check_link("https://example.com/page", session, "test-agent")
        assert result.status == "ok"
        assert result.http_code == 200
        calls = session.request.call_args_list
        assert calls[0][0][0] == "HEAD"
        assert calls[1][0][0] == "GET"

    def test_head_fallback_to_get_on_501(self):
        """When HEAD returns 501, should retry with GET."""
        session = MagicMock(spec=requests.Session)

        head_resp = _mock_response(501, "https://example.com/page")
        get_resp = _mock_response(200, "https://example.com/page")

        session.request.side_effect = [head_resp, get_resp]

        result = check_link("https://example.com/page", session, "test-agent")
        assert result.status == "ok"
        assert result.http_code == 200

    def test_soft_404_redirect_to_error_page(self):
        """A URL that redirects to the site's error page is classified as soft_404."""
        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(200, "https://example.com/404-page")

        result = check_link(
            "https://example.com/old-link",
            session,
            "test-agent",
            error_page_url="https://example.com/404-page",
        )
        assert result.status == "soft_404"
        assert result.http_code == 200
        assert result.final_url == "https://example.com/404-page"

    def test_404_page_itself_is_kept(self):
        """The actual 404 page URL returns 200 and should NOT be classified as soft_404."""
        session = MagicMock(spec=requests.Session)
        session.request.return_value = _mock_response(200, "https://example.com/404-page")

        result = check_link(
            "https://example.com/404-page",
            session,
            "test-agent",
            error_page_url="https://example.com/404-page",
        )
        assert result.status == "ok"
        assert result.http_code == 200
        assert result.final_url is None

    @patch("arkivari.validate.time.sleep")
    def test_transient_retry(self, mock_sleep: MagicMock):
        """Transient 503 is retried and succeeds on next attempt."""
        session = MagicMock(spec=requests.Session)

        err_resp = _mock_response(503, "https://example.com/page")
        ok_resp = _mock_response(200, "https://example.com/page")

        session.request.side_effect = [err_resp, ok_resp]

        result = check_link("https://example.com/page", session, "test-agent")
        assert result.status == "ok"
        assert result.http_code == 200
        mock_sleep.assert_called_once()

    @patch("arkivari.validate.time.sleep")
    def test_network_error_retry_exhausted(self, mock_sleep: MagicMock):
        """Network errors are retried up to MAX_RETRIES then classified."""
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("refused")

        result = check_link("https://example.com/page", session, "test-agent")
        assert result.status == "network_error"
        assert result.http_code == 0
        assert result.error is not None


# ---------------------------------------------------------------------------
# validate_urls (integration)
# ---------------------------------------------------------------------------

class TestValidateUrls:
    def test_filters_404_keeps_200(self):
        session = MagicMock(spec=requests.Session)
        robots = MagicMock(spec=["wait_for_crawl"])

        def mock_request(method, url, **kwargs):
            if "missing" in url:
                return _mock_response(404, url)
            return _mock_response(200, url)

        session.request.side_effect = mock_request
        # probe_error_page call via session.get
        probe_resp = _mock_response(404, "https://example.com/arkivari-probe-404-xxx")
        probe_resp.is_redirect = False
        session.get.return_value = probe_resp

        urls = [
            "https://example.com/good",
            "https://example.com/missing",
            "https://example.com/also-good",
        ]

        result = validate_urls(urls, session, "https://example.com", "test-agent", robots)

        assert "https://example.com/good" in result.urls
        assert "https://example.com/also-good" in result.urls
        assert "https://example.com/missing" not in result.urls
        assert result.stats.ok == 2
        assert result.stats.not_found == 1
        assert result.stats.total_checked == 3

    def test_includes_forbidden_and_auth(self):
        session = MagicMock(spec=requests.Session)
        robots = MagicMock(spec=["wait_for_crawl"])

        def mock_request(method, url, **kwargs):
            if "secret" in url:
                return _mock_response(403, url)
            if "login" in url:
                return _mock_response(401, url)
            return _mock_response(200, url)

        session.request.side_effect = mock_request
        probe_resp = _mock_response(404, "https://example.com/probe")
        probe_resp.is_redirect = False
        session.get.return_value = probe_resp

        urls = [
            "https://example.com/public",
            "https://example.com/secret",
            "https://example.com/login",
        ]

        result = validate_urls(urls, session, "https://example.com", "test-agent", robots)

        assert len(result.urls) == 3
        assert result.stats.ok == 1
        assert result.stats.forbidden == 1
        assert result.stats.auth_required == 1

    def test_soft_404_excluded(self):
        session = MagicMock(spec=requests.Session)
        robots = MagicMock(spec=["wait_for_crawl"])

        error_page = "https://example.com/not-found"

        # probe returns 200 with redirect to error page
        probe_resp = _mock_response(200, error_page)
        probe_resp.is_redirect = False
        session.get.return_value = probe_resp

        def mock_request(method, url, **kwargs):
            if "dead-link" in url:
                return _mock_response(200, error_page)
            return _mock_response(200, url)

        session.request.side_effect = mock_request

        urls = [
            "https://example.com/real-page",
            "https://example.com/dead-link",
        ]

        result = validate_urls(urls, session, "https://example.com", "test-agent", robots)

        assert "https://example.com/real-page" in result.urls
        assert "https://example.com/dead-link" not in result.urls
        assert result.stats.soft_404 == 1


# ---------------------------------------------------------------------------
# INCLUDABLE_STATUSES correctness
# ---------------------------------------------------------------------------

class TestIncludableStatuses:
    def test_expected_members(self):
        assert INCLUDABLE_STATUSES == frozenset(("ok", "redirected", "forbidden", "auth_required"))

    def test_not_found_excluded(self):
        assert "not_found" not in INCLUDABLE_STATUSES

    def test_gone_excluded(self):
        assert "gone" not in INCLUDABLE_STATUSES

    def test_soft_404_excluded(self):
        assert "soft_404" not in INCLUDABLE_STATUSES

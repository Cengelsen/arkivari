"""Tests for Save Page Now credential verification."""

from __future__ import annotations

from unittest.mock import MagicMock

from arkivari.archive import (
    check_ia_s3_credentials,
    check_spn_submit_auth,
    verify_spn_credentials,
)
from arkivari.spn_config import SpnConfig


def _auth_config() -> SpnConfig:
    return SpnConfig(
        authenticated=True,
        auth_method="s3",
        access_key="access",
        secret_key="secret",
        captures_per_minute=7,
        daily_cap=100_000,
        min_interval_seconds=60.0 / 7,
    )


class TestCheckIaS3Credentials:
    def test_authorized(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"authorized": True, "username": "alice"}
        session = MagicMock()
        session.get.return_value = response

        ok, err, username = check_ia_s3_credentials(_auth_config(), session)
        assert ok is True
        assert err is None
        assert username == "alice"

    def test_rejected(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"authorized": False}
        session = MagicMock()
        session.get.return_value = response

        ok, err, username = check_ia_s3_credentials(_auth_config(), session)
        assert ok is False
        assert err is not None
        assert username is None


class TestCheckSpnSubmitAuth:
    def test_job_id_success(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"job_id": "abc-123"}
        session = MagicMock()
        session.post.return_value = response

        ok, err = check_spn_submit_auth(_auth_config(), session, "test-agent")
        assert ok is True
        assert err is None

    def test_rate_limit_accepted(self) -> None:
        response = MagicMock()
        response.status_code = 429
        session = MagicMock()
        session.post.return_value = response

        ok, err = check_spn_submit_auth(_auth_config(), session, "test-agent")
        assert ok is True
        assert err is None

    def test_unauthorized(self) -> None:
        response = MagicMock()
        response.status_code = 401
        response.text = '{"message":"You need to be logged in to use Save Page Now."}'
        session = MagicMock()
        session.post.return_value = response

        ok, err = check_spn_submit_auth(_auth_config(), session, "test-agent")
        assert ok is False
        assert err is not None
        assert "401" in err

    def test_recent_capture_without_job_id_counts_as_auth_ok(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "url": "https://example.com/",
            "message": "Same snapshot is already available (recent capture).",
        }
        session = MagicMock()
        session.post.return_value = response

        ok, err = check_spn_submit_auth(_auth_config(), session, "test-agent")
        assert ok is True
        assert err is None


class TestCheckSpnSubmitAuthCookies:
    def test_cookie_probe_sends_cookie_header(self) -> None:
        from arkivari.archive import check_spn_submit_auth

        config = SpnConfig(
            authenticated=True,
            auth_method="cookies",
            access_key=None,
            secret_key=None,
            logged_in_sig="sig",
            logged_in_user="u%40ia.org",
            captures_per_minute=7,
            daily_cap=100_000,
            min_interval_seconds=1.0,
        )
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"job_id": "j-1"}
        session = MagicMock()
        session.post.return_value = response

        ok, err = check_spn_submit_auth(config, session, "test-agent")
        assert ok is True
        call_kwargs = session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers is not None
        assert "Cookie" in headers
        assert "logged-in-sig=sig" in headers["Cookie"]


class TestVerifySpnCredentials:
    def test_anonymous_skips(self) -> None:
        config = SpnConfig(
            authenticated=False,
            auth_method=None,
            access_key=None,
            secret_key=None,
            captures_per_minute=6,
            daily_cap=4000,
            min_interval_seconds=10.0,
        )
        ok, err, username = verify_spn_credentials(config, "agent", MagicMock())
        assert ok is True
        assert err is None
        assert username is None

    def test_s3_failure_short_circuits(self) -> None:
        session = MagicMock()
        s3_response = MagicMock()
        s3_response.status_code = 200
        s3_response.json.return_value = {"authorized": False}
        session.get.return_value = s3_response

        ok, err, username = verify_spn_credentials(_auth_config(), "agent", session)
        assert ok is False
        assert err is not None
        assert username is None
        session.post.assert_not_called()

    def test_cookie_auth_skips_s3_check(self) -> None:
        config = SpnConfig(
            authenticated=True,
            auth_method="cookies",
            access_key=None,
            secret_key=None,
            logged_in_sig="sig",
            logged_in_user="user%40example.org",
            captures_per_minute=7,
            daily_cap=100_000,
            min_interval_seconds=1.0,
        )
        spn_response = MagicMock()
        spn_response.status_code = 200
        spn_response.json.return_value = {"job_id": "probe"}
        session = MagicMock()
        session.post.return_value = spn_response

        ok, err, username = verify_spn_credentials(config, "agent", session)
        assert ok is True
        session.get.assert_not_called()
        assert username == "user%40example.org"
"""Tests for Save Page Now connectivity pre-check."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from arkivari.archive import check_spn_connectivity
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


class TestCheckSpnConnectivity:
    @patch("arkivari.archive.requests.Session")
    def test_success(self, mock_session_cls: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 200
        session = MagicMock()
        session.get.return_value = response
        mock_session_cls.return_value = session

        ok, err = check_spn_connectivity()
        assert ok is True
        assert err is None

    @patch("arkivari.archive.requests.Session")
    def test_connection_error(self, mock_session_cls: MagicMock) -> None:
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectionError("refused")
        mock_session_cls.return_value = session

        ok, err = check_spn_connectivity()
        assert ok is False
        assert err is not None

    @patch("arkivari.archive.requests.Session")
    def test_http_502(self, mock_session_cls: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 502
        session = MagicMock()
        session.get.return_value = response
        mock_session_cls.return_value = session

        ok, err = check_spn_connectivity()
        assert ok is False
        assert "502" in (err or "")

    @patch("arkivari.archive.verify_spn_credentials")
    @patch("arkivari.archive.requests.Session")
    def test_authenticated_runs_credential_check(
        self,
        mock_session_cls: MagicMock,
        mock_verify: MagicMock,
    ) -> None:
        response = MagicMock()
        response.status_code = 200
        session = MagicMock()
        session.get.return_value = response
        mock_session_cls.return_value = session
        mock_verify.return_value = (True, None, "alice")

        ok, err = check_spn_connectivity(
            session=session,
            spn_config=_auth_config(),
            user_agent="test-agent",
        )
        assert ok is True
        assert err is None
        mock_verify.assert_called_once()

    @patch("arkivari.archive.verify_spn_credentials")
    @patch("arkivari.archive.requests.Session")
    def test_authenticated_auth_failure(
        self,
        mock_session_cls: MagicMock,
        mock_verify: MagicMock,
    ) -> None:
        response = MagicMock()
        response.status_code = 200
        session = MagicMock()
        session.get.return_value = response
        mock_session_cls.return_value = session
        mock_verify.return_value = (False, "bad keys", None)

        ok, err = check_spn_connectivity(
            session=session,
            spn_config=_auth_config(),
            user_agent="test-agent",
        )
        assert ok is False
        assert err == "bad keys"
"""Tests for Save Page Now configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arkivari.spn_config import (
    ANONYMOUS_DAILY_CAP,
    AUTHENTICATED_DAILY_CAP,
    ACCESS_KEY_ENV,
    SECRET_KEY_ENV,
    SPN_CAPTURE_OPTIONS,
    SpnConfig,
    build_spn_submit_data,
    load_dotenv,
    resolve_daily_limit,
    resolve_spn_config,
    spn_capture_post_fields,
)


class TestSpnCaptureOptions:
    def test_resolve_copies_options_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ACCESS_KEY_ENV, raising=False)
        monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
        config = resolve_spn_config()
        assert config.capture_options == SPN_CAPTURE_OPTIONS
        config.capture_options["capture_outlinks"] = True
        assert SPN_CAPTURE_OPTIONS["capture_outlinks"] is False

    def test_post_fields_only_enabled(self) -> None:
        fields = spn_capture_post_fields({"capture_all": True, "wacz": False})
        assert fields == {"capture_all": "1"}

    def test_build_submit_data_authenticated(self) -> None:
        config = SpnConfig(
            authenticated=True,
            access_key="a",
            secret_key="b",
            captures_per_minute=7,
            daily_cap=100_000,
            min_interval_seconds=1.0,
            capture_options={"capture_all": True, "capture_outlinks": True},
        )
        data = build_spn_submit_data("https://example.com/", config, authenticated=True)
        assert data["url"] == "https://example.com/"
        assert data["capture_all"] == "1"
        assert data["capture_outlinks"] == "1"
        assert data["skip_first_archive"] == "1"
        assert "wacz" not in data

    def test_build_submit_data_anonymous(self) -> None:
        config = resolve_spn_config(force_anonymous=True)
        data = build_spn_submit_data("https://example.com/", config, authenticated=False)
        assert data["url"] == "https://example.com/"
        assert "skip_first_archive" not in data


class TestResolveSpnConfig:
    def test_anonymous_without_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ACCESS_KEY_ENV, raising=False)
        monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
        config = resolve_spn_config()
        assert not config.authenticated
        assert config.daily_cap == ANONYMOUS_DAILY_CAP
        assert config.captures_per_minute == 3

    def test_authenticated_with_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ACCESS_KEY_ENV, "test-access")
        monkeypatch.setenv(SECRET_KEY_ENV, "test-secret")
        config = resolve_spn_config()
        assert config.authenticated
        assert config.daily_cap == AUTHENTICATED_DAILY_CAP
        assert config.captures_per_minute == 7

    def test_force_anonymous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ACCESS_KEY_ENV, "test-access")
        monkeypatch.setenv(SECRET_KEY_ENV, "test-secret")
        config = resolve_spn_config(force_anonymous=True)
        assert not config.authenticated


class TestResolveDailyLimit:
    def test_default_authenticated_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ACCESS_KEY_ENV, "a")
        monkeypatch.setenv(SECRET_KEY_ENV, "b")
        config = resolve_spn_config()
        assert resolve_daily_limit(None, config) == AUTHENTICATED_DAILY_CAP

    def test_bump_queue_from_anonymous_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ACCESS_KEY_ENV, "a")
        monkeypatch.setenv(SECRET_KEY_ENV, "b")
        config = resolve_spn_config()
        assert (
            resolve_daily_limit(None, config, queue_daily_limit=ANONYMOUS_DAILY_CAP)
            == AUTHENTICATED_DAILY_CAP
        )

    def test_user_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = resolve_spn_config(force_anonymous=True)
        assert resolve_daily_limit(500, config, user_set_daily_limit=True) == 500


class TestLoadDotenv:
    def test_loads_keys_from_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            f"{ACCESS_KEY_ENV}=from-file\n{SECRET_KEY_ENV}=secret-from-file\n",
            encoding="utf-8",
        )
        monkeypatch.delenv(ACCESS_KEY_ENV, raising=False)
        monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
        monkeypatch.chdir(tmp_path)
        load_dotenv()
        assert os.environ.get(ACCESS_KEY_ENV) == "from-file"

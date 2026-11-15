"""Tests for Save Page Now configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arkivari.spn_config import (
    ANONYMOUS_CAPTURES_PER_MINUTE,
    ANONYMOUS_DAILY_CAP,
    ANONYMOUS_MAX_CONCURRENT,
    AUTHENTICATED_DAILY_CAP,
    AUTHENTICATED_MAX_CONCURRENT,
    ACCESS_KEY_ENV,
    LOGGED_IN_SIG_ENV,
    LOGGED_IN_USER_ENV,
    PREFER_COOKIES_ENV,
    SECRET_KEY_ENV,
    SPN_CAPTURE_OPTIONS,
    SpnConfig,
    apply_spn_auth_headers,
    build_spn_submit_data,
    cookie_header,
    discover_dotenv_path,
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
        config.capture_options["capture_screenshot"] = True
        assert SPN_CAPTURE_OPTIONS["capture_screenshot"] is False

    def test_post_fields_only_enabled(self) -> None:
        fields = spn_capture_post_fields({"capture_all": True, "wacz": False})
        assert fields == {"capture_all": "1"}

    def test_build_submit_data_authenticated(self) -> None:
        config = SpnConfig(
            authenticated=True,
            auth_method="s3",
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
        assert config.captures_per_minute == ANONYMOUS_CAPTURES_PER_MINUTE
        assert config.max_concurrent == ANONYMOUS_MAX_CONCURRENT

    def test_authenticated_with_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ACCESS_KEY_ENV, "test-access")
        monkeypatch.setenv(SECRET_KEY_ENV, "test-secret")
        config = resolve_spn_config()
        assert config.authenticated
        assert config.auth_method == "s3"
        assert config.daily_cap == AUTHENTICATED_DAILY_CAP
        assert config.captures_per_minute == 7
        assert config.max_concurrent == AUTHENTICATED_MAX_CONCURRENT

    def test_authenticated_with_cookies_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ACCESS_KEY_ENV, raising=False)
        monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
        monkeypatch.setenv(LOGGED_IN_SIG_ENV, "sig-value")
        monkeypatch.setenv(LOGGED_IN_USER_ENV, "user%40example.org")
        config = resolve_spn_config()
        assert config.authenticated
        assert config.auth_method == "cookies"
        assert config.mode_label == "authenticated (cookies)"
        assert cookie_header(config) == (
            "logged-in-sig=sig-value; logged-in-user=user%40example.org"
        )

    def test_prefers_s3_when_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ACCESS_KEY_ENV, "a")
        monkeypatch.setenv(SECRET_KEY_ENV, "b")
        monkeypatch.setenv(LOGGED_IN_SIG_ENV, "sig")
        monkeypatch.setenv(LOGGED_IN_USER_ENV, "u")
        config = resolve_spn_config()
        assert config.auth_method == "s3"

    def test_prefers_cookies_when_flag_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ACCESS_KEY_ENV, "a")
        monkeypatch.setenv(SECRET_KEY_ENV, "b")
        monkeypatch.setenv(LOGGED_IN_SIG_ENV, "sig")
        monkeypatch.setenv(LOGGED_IN_USER_ENV, "u")
        monkeypatch.setenv(PREFER_COOKIES_ENV, "1")
        config = resolve_spn_config()
        assert config.auth_method == "cookies"

    def test_apply_spn_auth_headers_cookie(self) -> None:
        config = SpnConfig(
            authenticated=True,
            auth_method="cookies",
            access_key=None,
            secret_key=None,
            logged_in_sig="abc",
            logged_in_user="me%40archive.org",
            captures_per_minute=7,
            daily_cap=100_000,
            min_interval_seconds=1.0,
        )
        headers: dict[str, str] = {}
        apply_spn_auth_headers(headers, config)
        assert "Cookie" in headers
        assert "Authorization" not in headers

    def test_force_anonymous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ACCESS_KEY_ENV, "test-access")
        monkeypatch.setenv(SECRET_KEY_ENV, "test-secret")
        config = resolve_spn_config(force_anonymous=True)
        assert not config.authenticated
        assert config.max_concurrent == ANONYMOUS_MAX_CONCURRENT


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


class TestDiscoverDotenvPath:
    def test_explicit_path(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("X=1\n", encoding="utf-8")
        assert discover_dotenv_path(env_file) == env_file
        assert discover_dotenv_path(tmp_path / "missing") is None

    def test_finds_cwd_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("X=1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert discover_dotenv_path() == env_file

    def test_finds_package_root_from_subdir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import arkivari.spn_config as spn_config_module

        repo = tmp_path / "repo"
        sub = repo / "sub"
        sub.mkdir(parents=True)
        env_file = repo / ".env"
        env_file.write_text("X=1\n", encoding="utf-8")
        monkeypatch.setattr(spn_config_module, "_PACKAGE_ROOT", repo)
        monkeypatch.chdir(sub)
        assert discover_dotenv_path() == env_file


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

    def test_spn_keys_in_file_override_shell(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            f"{ACCESS_KEY_ENV}=file-access\n{SECRET_KEY_ENV}=file-secret\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(ACCESS_KEY_ENV, "shell-access")
        monkeypatch.setenv(SECRET_KEY_ENV, "shell-secret")
        monkeypatch.chdir(tmp_path)
        load_dotenv()
        config = resolve_spn_config()
        assert config.authenticated
        assert config.access_key == "file-access"
        assert config.secret_key == "file-secret"

    def test_other_keys_do_not_override_shell(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER_VAR=from-file\n", encoding="utf-8")
        monkeypatch.setenv("OTHER_VAR", "from-shell")
        monkeypatch.chdir(tmp_path)
        load_dotenv()
        assert os.environ.get("OTHER_VAR") == "from-shell"

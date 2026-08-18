"""Tests for robothor.config's workspace .env file loading.

``robothor init`` writes ``$ROBOTHOR_WORKSPACE/.env`` (see
``robothor/setup.py``'s ``write_env_file``) but until now nothing read it
back — every post-init command ran against a bare ``os.environ``. These
tests cover the loader that fixes that:

- the real environment always wins over values in the file
- a missing or unreadable file is silently fine
- malformed file content never raises
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from robothor.config import (
    _env_file_path,
    _load_env_file,
    _parse_env_file,
    get_config,
    reset_config,
)


@pytest.fixture(autouse=True)
def clean_config():
    reset_config()
    yield
    reset_config()


class TestParseEnvFile:
    def test_parses_simple_assignment(self):
        assert _parse_env_file("FOO=bar") == {"FOO": "bar"}

    def test_ignores_comments_and_blank_lines(self):
        text = "\n".join(
            [
                "# a comment",
                "",
                "   ",
                "FOO=bar",
                "  # indented comment",
            ]
        )
        assert _parse_env_file(text) == {"FOO": "bar"}

    def test_strips_matching_double_quotes(self):
        assert _parse_env_file('FOO="bar baz"') == {"FOO": "bar baz"}

    def test_strips_matching_single_quotes(self):
        assert _parse_env_file("FOO='bar baz'") == {"FOO": "bar baz"}

    def test_leaves_mismatched_quote_alone(self):
        assert _parse_env_file("FOO='bar") == {"FOO": "'bar"}

    def test_strips_surrounding_whitespace(self):
        assert _parse_env_file("  FOO = bar  ") == {"FOO": "bar"}

    def test_handles_empty_value(self):
        assert _parse_env_file("FOO=") == {"FOO": ""}

    def test_skips_lines_without_equals(self):
        assert _parse_env_file("not a valid line\nFOO=bar") == {"FOO": "bar"}

    def test_skips_lines_with_empty_key(self):
        assert _parse_env_file("=bar\nFOO=baz") == {"FOO": "baz"}

    def test_last_assignment_wins_within_file(self):
        assert _parse_env_file("FOO=first\nFOO=second") == {"FOO": "second"}

    @pytest.mark.parametrize(
        "junk",
        [
            "\x00\x01\x02",
            "=" * 500,
            "FOO=" + "x" * 10000,
            "\n".join(f"KEY{i}=val{i}" for i in range(500)),
            "===",
            "FOO==bar==baz",
            "no equals sign anywhere in this whole line",
            "🎉=🎊",
            "\r\n\r\nFOO=bar\r\n",
        ],
    )
    def test_never_raises_on_arbitrary_content(self, junk):
        _parse_env_file(junk)  # must not raise


class TestLoadEnvFile:
    def test_missing_file_is_silently_fine(self, tmp_path):
        _load_env_file(tmp_path / "does-not-exist" / ".env")  # must not raise

    def test_unreadable_file_is_silently_fine(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("ROBOTHOR_TEST_DOTENV_KEY=bar\n")
        path.chmod(0)
        try:
            _load_env_file(path)  # must not raise
        finally:
            path.chmod(0o644)

    def test_sets_environ_from_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TEST_DOTENV_KEY", raising=False)
        path = tmp_path / ".env"
        path.write_text("ROBOTHOR_TEST_DOTENV_KEY=from-file\n")
        _load_env_file(path)
        assert os.environ["ROBOTHOR_TEST_DOTENV_KEY"] == "from-file"

    def test_real_environ_wins_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TEST_DOTENV_KEY", "from-real-env")
        path = tmp_path / ".env"
        path.write_text("ROBOTHOR_TEST_DOTENV_KEY=from-file\n")
        _load_env_file(path)
        assert os.environ["ROBOTHOR_TEST_DOTENV_KEY"] == "from-real-env"

    def test_idempotent_double_load(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TEST_DOTENV_KEY", raising=False)
        path = tmp_path / ".env"
        path.write_text("ROBOTHOR_TEST_DOTENV_KEY=from-file\n")
        _load_env_file(path)
        _load_env_file(path)  # must not raise; result unchanged
        assert os.environ["ROBOTHOR_TEST_DOTENV_KEY"] == "from-file"

    def test_malformed_file_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TEST_DOTENV_KEY", raising=False)
        path = tmp_path / ".env"
        path.write_bytes(b"\x00\x01\x02not=valid\n=====\nROBOTHOR_TEST_DOTENV_KEY=bar\n")
        _load_env_file(path)  # must not raise
        assert os.environ.get("ROBOTHOR_TEST_DOTENV_KEY") == "bar"


class TestEnvFilePath:
    def test_defaults_to_home_robothor(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_WORKSPACE", raising=False)
        assert _env_file_path() == Path.home() / "robothor" / ".env"

    def test_respects_workspace_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        assert _env_file_path() == tmp_path / ".env"


class TestConfigIntegration:
    """End-to-end: get_config() reflects the same environ that _load_env_file
    populates, exactly as happens once at config.py import time."""

    def test_get_config_reflects_env_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ROBOTHOR_OWNER_NAME", raising=False)
        (tmp_path / ".env").write_text("ROBOTHOR_OWNER_NAME=FileOwner\n")

        _load_env_file(_env_file_path())
        cfg = get_config()
        assert cfg.owner_name == "FileOwner"

    def test_get_config_env_wins_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ROBOTHOR_OWNER_NAME", "RealEnvOwner")
        (tmp_path / ".env").write_text("ROBOTHOR_OWNER_NAME=FileOwner\n")

        _load_env_file(_env_file_path())
        cfg = get_config()
        assert cfg.owner_name == "RealEnvOwner"

    def test_stray_workspace_dotenv_does_not_shadow_systemd_env(self, tmp_path, monkeypatch):
        """CRITICAL: production runs with EnvironmentFile-provided env
        (systemd). A stray workspace .env left on the box must never shadow
        those real values — environ-wins guarantees that."""
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ROBOTHOR_DB_HOST", "systemd-real-host")
        (tmp_path / ".env").write_text("ROBOTHOR_DB_HOST=stray-file-host\n")

        _load_env_file(_env_file_path())
        cfg = get_config()
        assert cfg.db.host == "systemd-real-host"

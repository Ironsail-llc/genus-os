"""The environment a Claude Code subprocess sees: an allowlist, never a copy."""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine.coding import env as coding_env
from robothor.engine.coding.env import (
    TOKEN_ENV,
    build_claude_env,
    build_verify_env,
    job_config_dir,
    resolve_oauth_token,
)

#: What a systemd instance's engine process actually holds after load-secrets.
FLEET_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "xterm",
    "TZ": "UTC",
    "HOME": "/users/alice",
    "OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 40,
    "TELEGRAM_BOT_TOKEN": "123456:" + "b" * 35,
    "ROBOTHOR_TELEGRAM_BOT_TOKEN": "123456:" + "b" * 35,
    "ROBOTHOR_DB_PASSWORD": "hunter2",
    "ANTHROPIC_API_KEY": "sk-ant-" + "c" * 40,
    "GH_TOKEN": "ghp_" + "d" * 36,
    "GITHUB_TOKEN": "ghp_" + "d" * 36,
    "AWS_SECRET_ACCESS_KEY": "e" * 40,
    "DATABASE_URL": "postgresql://alice:hunter2@db/genus",
    "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent",
    "XDG_CONFIG_HOME": "/users/alice/.config",
    "ROBOTHOR_WORKSPACE": "/opt/robothor",
}


def test_job_config_dir_lives_under_xdg_config_robothor(tmp_path):
    base = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": "/users/alice"}
    assert (
        job_config_dir("job-1", base=base)
        == tmp_path / "xdg" / "robothor" / "claude-code" / "job-1"
    )
    assert job_config_dir("job-1", base={"HOME": "/users/alice"}) == Path(
        "/users/alice/.config/robothor/claude-code/job-1"
    )


def test_job_config_dir_refuses_a_path_traversing_job_id(tmp_path):
    with pytest.raises(ValueError):
        job_config_dir("../../etc", base={"XDG_CONFIG_HOME": str(tmp_path)})


def test_no_fleet_secret_reaches_claude(tmp_path):
    base = dict(FLEET_ENV, XDG_CONFIG_HOME=str(tmp_path))
    env = build_claude_env(job_id="job-1", oauth_token="sk-ant-oat01-" + "z" * 40, base=base)

    for leaked in (
        "OPENROUTER_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "ROBOTHOR_TELEGRAM_BOT_TOKEN",
        "ROBOTHOR_DB_PASSWORD",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
        "SSH_AUTH_SOCK",
    ):
        assert leaked not in env, leaked
    # And no VALUE leaked under some other name either.
    for secret in ("hunter2", "sk-or-v1-", "ghp_"):
        assert not any(secret in v for v in env.values()), secret


def test_claude_gets_its_token_a_private_home_and_working_essentials(tmp_path):
    base = dict(FLEET_ENV, XDG_CONFIG_HOME=str(tmp_path))
    env = build_claude_env(
        job_id="job-1",
        oauth_token="tok-123",
        base=base,
        git_identity=("Alice Example", "agent@example.com"),
    )
    home = tmp_path / "robothor" / "claude-code" / "job-1"

    assert env[TOKEN_ENV] == "tok-123"
    assert env["HOME"] == str(home)
    assert env["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
    assert home.is_dir()
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    assert env["PATH"] == FLEET_ENV["PATH"]
    assert env["LANG"] == "C.UTF-8" and env["LC_ALL"] == "C.UTF-8"
    assert env["GIT_AUTHOR_NAME"] == "Alice Example"
    assert env["GIT_COMMITTER_EMAIL"] == "agent@example.com"
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_github_token_only_when_explicitly_granted(tmp_path):
    base = dict(FLEET_ENV, XDG_CONFIG_HOME=str(tmp_path))
    without = build_claude_env(job_id="j", oauth_token="t", base=base)
    granted = build_claude_env(job_id="j", oauth_token="t", base=base, github_token="ghp_granted")

    assert "GH_TOKEN" not in without
    assert granted["GH_TOKEN"] == "ghp_granted"


def test_verify_env_carries_no_token_at_all(tmp_path):
    base = dict(FLEET_ENV, XDG_CONFIG_HOME=str(tmp_path))
    env = build_verify_env(job_id="j", base=base)

    assert TOKEN_ENV not in env
    assert "GH_TOKEN" not in env
    assert env["HOME"].endswith("/robothor/claude-code/j")


def test_host_auth_mode_keeps_the_service_users_own_login(tmp_path):
    base = dict(FLEET_ENV, XDG_CONFIG_HOME=str(tmp_path), ROBOTHOR_CLAUDE_CODE_AUTH="host")
    env = build_claude_env(job_id="j", oauth_token=None, base=base)

    assert env["HOME"] == "/users/alice"
    assert "CLAUDE_CONFIG_DIR" not in env
    assert TOKEN_ENV not in env
    assert "OPENROUTER_API_KEY" not in env


def test_token_is_resolved_vault_first_through_the_accessor(monkeypatch):
    calls = []

    def fake_resolve(name, *, tenant_id, **_):
        calls.append((name, tenant_id))
        from robothor.secrets import ResolvedSecret

        return ResolvedSecret("from-vault", "vault")

    monkeypatch.setattr(coding_env, "resolve_secret", fake_resolve)

    assert resolve_oauth_token("test-tenant") == "from-vault"
    assert calls == [(TOKEN_ENV, "test-tenant")]


def test_auth_defaults_to_auto():
    from robothor.settings.model import CodingSettings

    assert CodingSettings().claude_code_auth == "auto"


def test_auto_without_a_stored_token_uses_the_hosts_claude_login(tmp_path):
    """No vault token: the job runs on the Claude Code login already on the box."""
    base = dict(FLEET_ENV, XDG_CONFIG_HOME=str(tmp_path), ROBOTHOR_CLAUDE_CODE_AUTH="auto")
    env = build_claude_env(job_id="j", oauth_token=None, base=base)

    assert env["HOME"] == "/users/alice"
    assert "CLAUDE_CONFIG_DIR" not in env
    assert TOKEN_ENV not in env
    assert "OPENROUTER_API_KEY" not in env


def test_auto_with_a_stored_token_prefers_the_token(tmp_path):
    base = dict(FLEET_ENV, XDG_CONFIG_HOME=str(tmp_path), ROBOTHOR_CLAUDE_CODE_AUTH="auto")
    env = build_claude_env(job_id="j", oauth_token="tok", base=base)

    assert env[TOKEN_ENV] == "tok"
    assert env["HOME"].endswith("/robothor/claude-code/j")


def test_host_login_present_reads_the_credentials_file(tmp_path):
    from robothor.engine.coding.env import host_login_present

    assert host_login_present({"HOME": str(tmp_path)}) is False
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    assert host_login_present({"HOME": str(tmp_path)}) is True

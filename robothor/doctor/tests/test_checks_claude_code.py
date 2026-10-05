"""doctor: is the Claude Code driver usable on this box?"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from robothor.doctor.checks import claude_code as cc_checks
from robothor.doctor.registry import builtin_checks
from robothor.doctor.tests.conftest import make_ctx
from robothor.engine.coding import sandbox as _sandbox
from robothor.engine.coding.runner import ClaudeCodeError

#: The real functions, captured before the autouse stub replaces them.
_REAL_SANDBOX_PROBLEMS = _sandbox.sandbox_problems
_REAL_UNWRITABLE = _sandbox.unwritable_login_paths


@pytest.fixture(autouse=True)
def _sandbox_ready(monkeypatch):
    """Every test starts on a host where the sandbox runs and ~/.claude is writable."""
    from robothor.engine.coding import sandbox

    monkeypatch.setattr(sandbox, "sandbox_problems", lambda: [])
    monkeypatch.setattr(sandbox, "unwritable_login_paths", lambda home=None: [])


def _run(**ctx_kw):
    check = next(c for c in cc_checks.CHECKS if c.id == "claude_code.ready")
    return asyncio.run(check.run(make_ctx(**ctx_kw)))


def test_registered():
    assert "claude_code.ready" in {c.id for c in builtin_checks()}


def test_skips_when_neither_cli_nor_token_exists():
    with (
        patch(
            "robothor.engine.coding.runner.resolve_claude_binary",
            side_effect=ClaudeCodeError("nope"),
        ),
        patch("robothor.secrets.secret_source", return_value="missing"),
        patch("robothor.engine.coding.env.host_login_present", return_value=False),
    ):
        result = _run()
    assert result.status == "skip"


def test_fails_when_a_token_exists_but_the_cli_does_not():
    with (
        patch(
            "robothor.engine.coding.runner.resolve_claude_binary",
            side_effect=ClaudeCodeError("nope"),
        ),
        patch("robothor.secrets.secret_source", return_value="vault"),
    ):
        result = _run()
    assert result.status == "fail"
    assert "CLI" in result.detail


def test_fails_when_the_cli_exists_but_no_token_and_no_host_login():
    with (
        patch("robothor.engine.coding.runner.resolve_claude_binary", return_value="/opt/claude"),
        patch("robothor.engine.coding.probe.cli_version", new=AsyncMock(return_value="2.1.289")),
        patch("robothor.secrets.secret_source", return_value="missing"),
        patch("robothor.engine.coding.env.host_login_present", return_value=False),
    ):
        result = _run()
    assert result.status == "fail"
    assert "claude-code login" in result.detail


def test_passes_with_cli_and_token_and_never_prints_the_value():
    with (
        patch("robothor.engine.coding.runner.resolve_claude_binary", return_value="/opt/claude"),
        patch("robothor.engine.coding.probe.cli_version", new=AsyncMock(return_value="2.1.289")),
        patch("robothor.secrets.secret_source", return_value="vault"),
    ):
        result = _run()
    assert result.status == "pass"
    assert "2.1.289" in result.detail and "vault" in result.detail


def test_passes_on_the_hosts_claude_login_without_a_token(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_CLAUDE_CODE_AUTH", raising=False)
    with (
        patch("robothor.engine.coding.runner.resolve_claude_binary", return_value="/opt/claude"),
        patch("robothor.engine.coding.probe.cli_version", new=AsyncMock(return_value="2.1.289")),
        patch("robothor.secrets.secret_source", return_value="missing"),
        patch("robothor.engine.coding.env.host_login_present", return_value=True),
    ):
        result = _run()
    assert result.status == "pass"
    assert "host" in result.detail


def _ready_patches(source="vault"):
    return (
        patch("robothor.engine.coding.runner.resolve_claude_binary", return_value="/opt/claude"),
        patch("robothor.engine.coding.probe.cli_version", new=AsyncMock(return_value="2.1.289")),
        patch("robothor.secrets.secret_source", return_value=source),
        patch("robothor.engine.coding.env.host_login_present", return_value=True),
    )


def test_fails_when_the_bash_sandbox_cannot_run(monkeypatch):
    """failIfUnavailable makes every job fail on such a host: say so up front."""
    from robothor.engine.coding import sandbox

    monkeypatch.setattr(sandbox, "sandbox_problems", lambda: ["socat is not installed"])
    a, b, c, d = _ready_patches()
    with a, b, c, d:
        result = _run()
    assert result.status == "fail"
    assert "socat" in result.detail and "sandbox" in result.detail


def test_fails_when_the_host_login_is_not_writable_by_the_engine(monkeypatch):
    from robothor.engine.coding import sandbox

    monkeypatch.delenv("ROBOTHOR_CLAUDE_CODE_AUTH", raising=False)
    monkeypatch.setattr(
        sandbox, "unwritable_login_paths", lambda home=None: ["/users/alice/.claude"]
    )
    a, b, c, d = _ready_patches(source="missing")
    with a, b, c, d:
        result = _run()
    assert result.status == "fail"
    assert "/users/alice/.claude" in result.detail
    assert "zz-claude-code.conf" in result.detail


def test_an_unwritable_login_does_not_matter_with_a_token(monkeypatch):
    from robothor.engine.coding import sandbox

    monkeypatch.setattr(
        sandbox, "unwritable_login_paths", lambda home=None: ["/users/alice/.claude"]
    )
    a, b, c, d = _ready_patches(source="vault")
    with a, b, c, d:
        result = _run()
    assert result.status == "pass"


def test_sandbox_problems_names_missing_binaries(monkeypatch):
    from robothor.engine.coding import sandbox

    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    problems = _REAL_SANDBOX_PROBLEMS()
    assert any("bwrap" in p for p in problems)
    assert any("socat" in p for p in problems)


def test_unwritable_login_paths_reports_a_read_only_claude_dir(tmp_path, monkeypatch):
    from robothor.engine.coding import sandbox

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text("{}")
    real_access = sandbox.os.access
    monkeypatch.setattr(
        sandbox.os,
        "access",
        lambda p, mode: False if str(p).endswith(".claude") else real_access(p, mode),
    )
    assert _REAL_UNWRITABLE(tmp_path) == [str(tmp_path / ".claude")]

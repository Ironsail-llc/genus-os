"""doctor: is the Claude Code driver usable on this box?"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from robothor.doctor.checks import claude_code as cc_checks
from robothor.doctor.registry import builtin_checks
from robothor.doctor.tests.conftest import make_ctx
from robothor.engine.coding.runner import ClaudeCodeError


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


def test_fails_when_the_cli_exists_but_no_token():
    with (
        patch("robothor.engine.coding.runner.resolve_claude_binary", return_value="/opt/claude"),
        patch("robothor.engine.coding.probe.cli_version", new=AsyncMock(return_value="2.1.289")),
        patch("robothor.secrets.secret_source", return_value="missing"),
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

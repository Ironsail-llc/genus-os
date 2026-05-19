"""Tests for Codex subscription provider CLI commands."""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import AsyncMock, MagicMock, patch

from robothor.cli.codex import cmd_codex


def test_codex_status_prints_login_status(capsys) -> None:
    args = Namespace(codex_command="status")
    with patch(
        "robothor.engine.codex_provider.login_status",
        new=AsyncMock(return_value="Logged in using ChatGPT"),
    ):
        rc = cmd_codex(args)

    assert rc == 0
    assert "Logged in using ChatGPT" in capsys.readouterr().out


def test_codex_doctor_fails_when_subscription_auth_missing(capsys) -> None:
    args = Namespace(codex_command="doctor")
    with patch(
        "robothor.engine.codex_provider.ensure_chatgpt_login",
        new=AsyncMock(side_effect=RuntimeError("not ChatGPT")),
    ):
        rc = cmd_codex(args)

    assert rc == 1
    assert "not ready" in capsys.readouterr().out


def test_codex_login_invokes_codex_without_openai_api_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    args = Namespace(codex_command="login", with_access_token=False)

    with patch("subprocess.call", return_value=0) as call:
        rc = cmd_codex(args)

    assert rc == 0
    called_env = call.call_args.kwargs["env"]
    assert call.call_args.args[0] == ["codex", "login"]
    assert "OPENAI_API_KEY" not in called_env


def test_codex_test_runs_provider_call(capsys) -> None:
    args = Namespace(
        codex_command="test",
        model="codex/gpt-5.5",
        prompt="ping",
        timeout=1,
    )
    response = MagicMock()
    response.choices[0].message.content = "pong"

    with patch("robothor.engine.codex_provider.acompletion", new=AsyncMock(return_value=response)):
        rc = cmd_codex(args)

    assert rc == 0
    assert "pong" in capsys.readouterr().out

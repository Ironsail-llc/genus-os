"""`robothor claude-code login|status` — the token goes to the vault, the ping proves it."""

from __future__ import annotations

import io
from argparse import Namespace
from unittest.mock import AsyncMock, patch

from robothor.cli.claude_code import cmd_claude_code
from robothor.engine.coding.runner import ClaudeResult


def _ok(**kw) -> ClaudeResult:
    base = {
        "session_id": "s",
        "is_error": False,
        "subtype": "success",
        "result_text": "ok",
        "total_cost_usd": 0.01,
        "num_turns": 1,
    }
    base.update(kw)
    return ClaudeResult(**base)


def test_login_from_stdin_stores_the_token_in_the_vault_and_pings(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-ant-oat01-example-token\n"))
    stored = {}

    def fake_set(key, value, *, category="credential", tenant_id="default"):
        stored.update(key=key, value=value, category=category, tenant_id=tenant_id)

    with (
        patch("robothor.vault.set", side_effect=fake_set),
        patch("robothor.engine.coding.probe.ping", new=AsyncMock(return_value=_ok())) as ping,
    ):
        rc = cmd_claude_code(
            Namespace(claude_code_command="login", token_stdin=True, no_ping=False)
        )

    out = capsys.readouterr().out
    assert rc == 0, out
    assert stored["value"] == "sk-ant-oat01-example-token"
    # The canonical row: where `vault_set` and `genus secrets migrate` write it,
    # and the first place the accessor looks for CLAUDE_CODE_OAUTH_TOKEN.
    from robothor.vault.naming import vault_keys_for_env_name

    assert stored["key"] == vault_keys_for_env_name("CLAUDE_CODE_OAUTH_TOKEN")[0]
    assert ping.await_count == 1
    assert "sk-ant-oat01-example-token" not in out  # never echoed


def test_login_refuses_an_empty_token(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    with patch("robothor.vault.set") as vault_set:
        rc = cmd_claude_code(Namespace(claude_code_command="login", token_stdin=True, no_ping=True))
    assert rc == 1
    vault_set.assert_not_called()


def test_login_without_stdin_runs_setup_token_then_asks_for_the_paste(monkeypatch) -> None:
    with (
        patch("subprocess.call", return_value=0) as call,
        patch("getpass.getpass", return_value="pasted-token"),
        patch("robothor.vault.set") as vault_set,
        patch("robothor.engine.coding.runner.resolve_claude_binary", return_value="/opt/claude"),
    ):
        rc = cmd_claude_code(
            Namespace(claude_code_command="login", token_stdin=False, no_ping=True)
        )
    assert rc == 0
    assert call.call_args.args[0] == ["/opt/claude", "setup-token"]
    assert vault_set.call_args.args[1] == "pasted-token"


def test_status_reports_source_and_a_failed_ping(capsys) -> None:
    with (
        patch(
            "robothor.engine.coding.probe.cli_version",
            new=AsyncMock(return_value="2.1.289 (Claude Code)"),
        ),
        patch("robothor.secrets.secret_source", return_value="vault"),
        patch(
            "robothor.engine.coding.probe.ping",
            new=AsyncMock(return_value=_ok(is_error=True, subtype="no_result", result_text="401")),
        ),
    ):
        rc = cmd_claude_code(Namespace(claude_code_command="status", no_ping=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "2.1.289" in out and "vault" in out and "401" in out


def test_status_passes_on_a_good_ping(capsys) -> None:
    with (
        patch("robothor.engine.coding.probe.cli_version", new=AsyncMock(return_value="2.1.289")),
        patch("robothor.secrets.secret_source", return_value="vault"),
        patch("robothor.engine.coding.probe.ping", new=AsyncMock(return_value=_ok())),
    ):
        rc = cmd_claude_code(Namespace(claude_code_command="status", no_ping=False))
    assert rc == 0
    assert "ok" in capsys.readouterr().out

"""The link an operator actually follows out of a terminal.

Everything else in this PR is reachable only if this line gets printed, so it
has its own tests: `genus init` must end with a URL carrying a live token, and
`genus auth setup-link` must be able to mint another one on a box nobody can
re-run init on.

Two details that look cosmetic and are not. A headless box has to be told how
to reach the port (``ssh -L``), because the printed ``http://127.0.0.1:3004``
is a loopback address on the SERVER and an operator who pastes it into their
own browser gets nothing. And the token must never reach a log — so it is
asserted here by shape, never by value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from robothor import setup_token


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No network, no real HOME, no real owner identity — and no database.

    ``setup_complete`` asks PostgreSQL whether an owner exists and fails CLOSED
    when it cannot, so without this stub every test here would depend on a
    running database and would flip to "already set up" in a container that has
    none. The one test about the refusal overrides it.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(tmp_path / "home" / "owner.yaml"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("robothor.auth.accounts.owner_account_exists", lambda *a, **k: False)
    yield


def _redacted(line: str) -> str:
    """The printed line with the token's value removed, so an assertion about
    the line cannot become a copy of the credential."""
    head, _, _tail = line.partition("token=")
    return head + "token=<redacted>"


class TestInitPrintsTheLink:
    def test_prints_a_setup_url_with_a_live_token(self, tmp_path, capsys, monkeypatch):
        import robothor.setup as setup_mod

        monkeypatch.setattr(setup_mod.httpx, "get", MagicMock(side_effect=Exception("no network")))
        workspace = tmp_path / "robothor"
        args = SimpleNamespace(
            yes=True, docker=False, skip_models=True, skip_db=True, workspace=str(workspace)
        )

        assert setup_mod.run_init(args) == 0

        out = capsys.readouterr().out
        link = next(line for line in out.splitlines() if "/setup?token=" in line)
        assert _redacted(link.strip()).endswith("/setup?token=<redacted>")

        token = link.strip().partition("token=")[2]
        assert setup_token.verify_setup_token(workspace, token) is True

    def test_the_token_file_holds_no_plaintext(self, tmp_path, capsys, monkeypatch):
        import robothor.setup as setup_mod

        monkeypatch.setattr(setup_mod.httpx, "get", MagicMock(side_effect=Exception("no network")))
        workspace = tmp_path / "robothor"
        args = SimpleNamespace(
            yes=True, docker=False, skip_models=True, skip_db=True, workspace=str(workspace)
        )
        setup_mod.run_init(args)

        out = capsys.readouterr().out
        token = next(line for line in out.splitlines() if "/setup?token=" in line).strip()
        token = token.partition("token=")[2]

        stored = setup_token.token_path(workspace).read_text(encoding="utf-8")
        assert token not in stored

    def test_a_non_tty_run_also_gets_the_port_forward_line(self, tmp_path, capsys, monkeypatch):
        """`genus init` under `--yes` is how a container or a script installs,
        and there is no browser on that box."""
        import robothor.setup as setup_mod

        monkeypatch.setattr(setup_mod.httpx, "get", MagicMock(side_effect=Exception("no network")))
        monkeypatch.setattr(setup_mod.sys.stdin, "isatty", lambda: False, raising=False)
        workspace = tmp_path / "robothor"
        args = SimpleNamespace(
            yes=True, docker=False, skip_models=True, skip_db=True, workspace=str(workspace)
        )
        setup_mod.run_init(args)

        out = capsys.readouterr().out
        assert "ssh -L 3004:127.0.0.1:3004" in out

    def test_a_failed_token_mint_does_not_fail_init(self, tmp_path, capsys, monkeypatch):
        """An unwritable workspace is a problem the operator should hear about,
        not a reason to lose a completed install."""
        import robothor.setup as setup_mod

        monkeypatch.setattr(setup_mod.httpx, "get", MagicMock(side_effect=Exception("no network")))
        monkeypatch.setattr(
            setup_mod.setup_token,
            "create_setup_token",
            MagicMock(side_effect=OSError("read-only filesystem")),
        )
        workspace = tmp_path / "robothor"
        args = SimpleNamespace(
            yes=True, docker=False, skip_models=True, skip_db=True, workspace=str(workspace)
        )

        assert setup_mod.run_init(args) == 0
        assert "genus auth setup-link" in capsys.readouterr().out


class TestAuthSetupLink:
    def test_mints_a_fresh_token_and_prints_a_link(self, tmp_path, capsys, monkeypatch):
        from robothor.cli.auth import cmd_auth

        workspace = tmp_path / "robothor"
        workspace.mkdir()
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))

        rc = cmd_auth(
            SimpleNamespace(auth_command="setup-link", host=None, json_output=False, ttl=None)
        )

        assert rc == 0
        out = capsys.readouterr().out
        token = next(line for line in out.splitlines() if "/setup?token=" in line)
        token = token.strip().partition("token=")[2]
        assert setup_token.verify_setup_token(workspace, token) is True

    def test_each_call_invalidates_the_previous_link(self, tmp_path, capsys, monkeypatch):
        from robothor.cli.auth import cmd_auth

        workspace = tmp_path / "robothor"
        workspace.mkdir()
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        args = SimpleNamespace(auth_command="setup-link", host=None, json_output=False, ttl=None)

        cmd_auth(args)
        first = capsys.readouterr().out.partition("token=")[2].split()[0]
        cmd_auth(args)
        second = capsys.readouterr().out.partition("token=")[2].split()[0]

        assert first != second
        assert setup_token.verify_setup_token(workspace, first) is False
        assert setup_token.verify_setup_token(workspace, second) is True

    def test_refuses_once_setup_is_complete(self, tmp_path, capsys, monkeypatch):
        """A link minted after an owner exists reaches routes that 404. Saying so
        is better than handing over a credential that does nothing."""
        from robothor.cli.auth import cmd_auth

        workspace = tmp_path / "robothor"
        workspace.mkdir()
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        monkeypatch.setattr("robothor.setup_token.setup_complete", lambda *a, **k: True)

        rc = cmd_auth(
            SimpleNamespace(auth_command="setup-link", host=None, json_output=False, ttl=None)
        )

        assert rc == 1
        assert not setup_token.token_path(workspace).exists()

    def test_a_named_host_gets_the_port_forward_line(self, tmp_path, capsys, monkeypatch):
        from robothor.cli.auth import cmd_auth

        workspace = tmp_path / "robothor"
        workspace.mkdir()
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))

        cmd_auth(
            SimpleNamespace(
                auth_command="setup-link",
                host="box.example.test",
                json_output=False,
                ttl=None,
            )
        )

        out = capsys.readouterr().out
        assert "ssh -L 3004:127.0.0.1:3004 box.example.test" in out

    def test_json_output_carries_the_link_and_the_expiry(self, tmp_path, capsys, monkeypatch):
        import json

        from robothor.cli.auth import cmd_auth

        workspace = tmp_path / "robothor"
        workspace.mkdir()
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))

        cmd_auth(SimpleNamespace(auth_command="setup-link", host=None, json_output=True, ttl=None))

        payload = json.loads(capsys.readouterr().out)
        assert "/setup?token=" in payload["url"]
        assert payload["expires_in_seconds"] > 0

    def test_ttl_is_honoured(self, tmp_path, capsys, monkeypatch):
        import json

        from robothor.cli.auth import cmd_auth

        workspace = tmp_path / "robothor"
        workspace.mkdir()
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))

        cmd_auth(SimpleNamespace(auth_command="setup-link", host=None, json_output=True, ttl="45s"))

        assert json.loads(capsys.readouterr().out)["expires_in_seconds"] == 45

    def test_no_workspace_is_an_error_not_a_link(self, tmp_path, capsys, monkeypatch):
        from robothor.cli.auth import cmd_auth

        monkeypatch.delenv("ROBOTHOR_WORKSPACE", raising=False)
        monkeypatch.setattr("robothor.settings.sources.workspace_path", lambda: None, raising=False)

        assert (
            cmd_auth(
                SimpleNamespace(auth_command="setup-link", host=None, json_output=False, ttl=None)
            )
            == 2
        )


class TestParserAcceptsIt:
    def test_genus_auth_setup_link_parses(self):
        """`docs/deployment.md` quotes this command, and
        `scripts/check_doc_commands.py` feeds it to the real parser."""
        from robothor.cli import _build_parser

        args = _build_parser().parse_args(["auth", "setup-link"])
        assert args.auth_command == "setup-link"

    def test_host_and_ttl_are_accepted(self):
        from robothor.cli import _build_parser

        args = _build_parser().parse_args(
            ["auth", "setup-link", "--host", "box.example.test", "--ttl", "2h", "--json"]
        )
        assert args.host == "box.example.test"
        assert args.ttl == "2h"
        assert args.json_output is True

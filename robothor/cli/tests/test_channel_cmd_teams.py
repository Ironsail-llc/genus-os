"""``genus channel add teams`` — the table takes a third channel, not a third branch.

The point of this file is narrower than Teams. ``add`` is a *table* of channels
and the fields each one needs, and a plugin channel has to be configurable
through it without the command growing a Teams-shaped branch — otherwise the
first channel that ships as a plugin is also the first channel whose credentials
have to be exported by hand.

The three values are not alike and are deliberately not stored alike. The client
secret is a credential and goes to the vault (or the 0600 env file). The
application id and the directory tenant id are not secrets — they are printed in
the Azure portal and appear in every activity — and putting them behind a master
key would make them unreadable by the doctor on an instance with no vault, which
is the mistake ``slack_verify_target`` and the SMTP host both avoided.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import pytest

from robothor.cli.channel import cmd_channel

#: Visibly fake. A real client secret here would be a credential in the repo.
FAKE_APP_PASSWORD = "not-a-real-teams-client-secret-0000"
APP_ID = "00000000-0000-0000-0000-00000000aaaa"
DIRECTORY_TENANT = "00000000-0000-0000-0000-00000000bbbb"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from robothor.settings import reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")) or name in DEPRECATED_ALIASES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


def _teams_add_args(**kwargs: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "channel_command": "add",
        "name": "teams",
        "json": False,
        "bot_token": None,
        "app_token": None,
        "app_password": None,
        "smtp_password": None,
        "verify_target": None,
        "app_id": None,
        "tenant_id": None,
        "to": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class _FakeVault:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def __call__(self, key: str, value: str) -> None:
        self.written[key] = value


@pytest.fixture
def vault(monkeypatch, tmp_path):
    from robothor.cli import channel as channel_cmd

    (tmp_path / ".vault-key").write_bytes(b"0" * 32)
    fake = _FakeVault()
    monkeypatch.setattr(channel_cmd, "_vault_set", fake)
    return fake


@pytest.fixture
def exported(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TEAMS_APP_PASSWORD", FAKE_APP_PASSWORD)


class TestTheThreeValuesLandWhereTheyBelong:
    def test_the_client_secret_goes_to_the_vault_under_the_name_the_channel_reads(
        self, vault, exported
    ):
        assert cmd_channel(_teams_add_args()) == 0
        assert set(vault.written) == {"channels/teams/app_password"}
        assert vault.written["channels/teams/app_password"] == FAKE_APP_PASSWORD

    def test_the_app_id_and_directory_tenant_are_settings_not_secrets(self, vault, exported):
        assert cmd_channel(_teams_add_args(app_id=APP_ID, tenant_id=DIRECTORY_TENANT)) == 0

        from robothor.settings import get_settings, reset_settings
        from robothor.settings.sources import config_yaml_path

        path = config_yaml_path()
        assert path is not None
        body = path.read_text(encoding="utf-8")
        assert APP_ID in body and DIRECTORY_TENANT in body
        assert "app_id" not in "".join(vault.written)

        reset_settings()
        channels = get_settings().channels
        assert channels.teams_app_id == APP_ID
        assert channels.teams_tenant_id == DIRECTORY_TENANT

    def test_it_never_prints_the_secret(self, vault, exported, capsys, caplog):
        with caplog.at_level("DEBUG"):
            cmd_channel(_teams_add_args())
        captured = capsys.readouterr()
        assert FAKE_APP_PASSWORD not in captured.out
        assert FAKE_APP_PASSWORD not in captured.err
        assert FAKE_APP_PASSWORD not in caplog.text
        assert "sha256:" in captured.out

    def test_the_secret_on_a_command_line_is_refused_and_nothing_is_stored(self, vault, capsys):
        assert cmd_channel(_teams_add_args(app_password=FAKE_APP_PASSWORD)) == 2
        assert vault.written == {}
        assert "ROBOTHOR_TEAMS_APP_PASSWORD" in capsys.readouterr().err


class TestItSaysWhenTheChannelIsNotInstalled:
    def test_storing_a_credential_for_an_uninstalled_channel_says_so(self, vault, exported, capsys):
        """`add` knows a channel that ships as a plugin, because the
        alternative is exporting its credentials by hand. Storing one for
        something that is not installed looks exactly like a working setup until
        the first delivery records failed:no_channel:teams."""
        assert cmd_channel(_teams_add_args()) == 0
        out = capsys.readouterr().out
        assert "not available on this instance yet" in out
        assert "ROBOTHOR_CHANNELS" in out

    def test_an_installed_and_armed_channel_gets_no_such_note(
        self, vault, exported, capsys, monkeypatch
    ):
        from robothor.engine import channels as channels_module

        monkeypatch.setattr(channels_module, "list_channels", lambda: {"teams": object()})
        assert cmd_channel(_teams_add_args()) == 0
        assert "not available on this instance" not in capsys.readouterr().out


class TestItIsATableAndNotABranch:
    def test_teams_is_one_row_in_the_same_two_tables_slack_and_email_use(self):
        from robothor.cli.channel import _ADDABLE, _ADDABLE_SETTINGS, _REFUSED_FLAGS

        assert "teams" in _ADDABLE
        assert "teams" in _ADDABLE_SETTINGS
        assert "teams" in _REFUSED_FLAGS

    def test_the_command_has_no_teams_shaped_branch(self):
        """A channel the table cannot express is a channel the next one cannot
        copy. If `teams` ever appears in the module's code, this seam has been
        abandoned for a special case."""
        import ast
        from pathlib import Path

        from robothor.cli import channel as channel_cmd

        tree = ast.parse(Path(channel_cmd.__file__).read_text(encoding="utf-8"))
        table_lines = set()
        for node in ast.walk(tree):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            if any(
                getattr(t, "id", "") in {"_ADDABLE", "_ADDABLE_SETTINGS", "_REFUSED_FLAGS"}
                for t in targets
            ):
                table_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == "teams"
            and node.lineno not in table_lines
        ]
        assert not offenders, f"a Teams special case outside the tables at line(s) {offenders}"


class TestTheAccessModeIsDeclared:
    def test_teams_has_a_mode_field_of_its_own_defaulting_to_pairing(self, monkeypatch):
        """A plugin channel would otherwise take ``channel_access_default``.
        Teams gets its own because an operator configuring one surface should
        not be silently changing the posture of another."""
        from robothor.engine.channels.access import access_mode
        from robothor.settings import reset_settings

        reset_settings()
        assert access_mode("teams") == "pairing"

        monkeypatch.setenv("ROBOTHOR_TEAMS_ACCESS", "open")
        reset_settings()
        assert access_mode("teams") == "open"

    def test_a_typo_in_the_mode_does_not_open_the_surface(self, monkeypatch):
        from robothor.engine.channels.access import access_mode
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_TEAMS_ACCESS", "opne")
        reset_settings()
        assert access_mode("teams") == "pairing"


class TestTheSettingsAreDeclaredConfiguration:
    @pytest.mark.parametrize(
        "env",
        [
            "ROBOTHOR_TEAMS_APP_ID",
            "ROBOTHOR_TEAMS_APP_PASSWORD",
            "ROBOTHOR_TEAMS_TENANT_ID",
            "ROBOTHOR_TEAMS_ACCESS",
            "ROBOTHOR_TEAMS_VERIFY_TARGET",
        ],
    )
    def test_every_teams_setting_is_in_the_registry(self, env):
        """A setting only reachable by knowing an environment variable name is
        one ``genus config`` can neither show nor document."""
        from robothor.settings.registry import field_index

        assert env in field_index()

    def test_the_client_secret_is_flagged_secret(self):
        from robothor.settings.registry import field_index

        entry = field_index()["ROBOTHOR_TEAMS_APP_PASSWORD"]
        assert getattr(entry, "secret", None) or entry.get("secret"), (
            "an unflagged secret is one `genus config` will print"
        )

"""A channel token stored by the migration must reach the channel's own reader.

Review R2 — C4 again, for channels, and with the same three tools attesting a
rotation that had not happened.

``ROBOTHOR_SLACK_BOT_TOKEN`` matched the ``<VENDOR>_TOKEN`` rule and
canonicalised to ``providers/robothor_slack_bot/api_key``. The Slack daemon
never looks there: ``slack_credentials`` passes
``vault_key="channels/slack/bot_token"`` explicitly, and the setup wizard writes
that key. So ``genus secrets migrate`` filed the token where nothing read it,
``genus secrets status`` and ``vault_set``'s ``readable_as`` both said it was
served from the vault, and the runbook's next step — delete the migrated entries
from the SOPS file — would have left the Slack daemon with no token at all.

So this file does not assert on ``vault_keys_for_env_name``. It runs the
migration and then asks the CHANNEL'S OWN READER, which is the only thing whose
answer matters.
"""

from __future__ import annotations

import argparse

import pytest

FAKE_BOT = "xoxb-FAKE-0000-0000-fakefakefakefake"
FAKE_APP = "xapp-1-FAKE-0000-fakefakefakefake"


@pytest.fixture
def stores(monkeypatch):
    """A vault double and an environment, both entirely fake."""
    from robothor import secrets as secrets_module
    from robothor import vault

    rows: dict[str, str] = {}
    monkeypatch.setattr(vault, "get", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(
        vault,
        "export_env",
        lambda **kw: {k.upper().replace("/", "_"): v for k, v in rows.items()},
    )
    monkeypatch.setattr(vault, "set", lambda key, value, **kw: rows.__setitem__(key, value))
    monkeypatch.setattr(vault, "list", lambda **kw: sorted(rows))
    secrets_module.reset_vault_availability()
    yield rows
    secrets_module.reset_vault_availability()


def _migrate(*names: str) -> None:
    """Migrate exactly these names.

    Scoped with ``--only`` rather than scanning, because a scan reads the
    environment of whoever runs the suite and would make the assertions depend
    on their machine. The scan path has its own tests in
    ``robothor/cli/tests/test_secrets_cmd.py``.
    """
    from robothor.cli.secrets_cmd import cmd_secrets

    cmd_secrets(
        argparse.Namespace(
            secrets_command="migrate",
            from_env=True,
            dry_run=False,
            only=list(names),
            tenant=None,
            overwrite=None,
        )
    )


def test_the_slack_daemon_reads_what_the_migration_stored(stores, monkeypatch, capsys):
    """The reader, not the mapping. ``slack_credentials`` is what starts the
    inbound bot, and it asks for an explicit vault key."""
    from robothor.engine.channels.slack_credentials import slack_credentials

    monkeypatch.setenv("ROBOTHOR_SLACK_BOT_TOKEN", FAKE_BOT)
    monkeypatch.setenv("ROBOTHOR_SLACK_APP_TOKEN", FAKE_APP)
    _migrate("ROBOTHOR_SLACK_BOT_TOKEN", "ROBOTHOR_SLACK_APP_TOKEN")
    capsys.readouterr()

    # The environment copy is now deleted from the secrets file, which is the
    # runbook's next step and the moment the old bug became an outage.
    monkeypatch.delenv("ROBOTHOR_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ROBOTHOR_SLACK_APP_TOKEN", raising=False)
    from robothor import secrets as secrets_module

    secrets_module.reset_secret_cache()

    found = slack_credentials()
    assert found.bot_token == FAKE_BOT, (
        "the Slack daemon cannot see the token the migration stored — after the "
        "runbook's SOPS shrink it would have no token at all"
    )
    assert found.app_token == FAKE_APP
    assert found.can_send and found.can_listen


def test_the_migration_files_a_channel_token_under_its_channel_key(stores, monkeypatch, capsys):
    monkeypatch.setenv("ROBOTHOR_SLACK_BOT_TOKEN", FAKE_BOT)
    _migrate("ROBOTHOR_SLACK_BOT_TOKEN")
    capsys.readouterr()
    assert "channels/slack/bot_token" in stores
    assert not [k for k in stores if k.startswith("providers/")], (
        f"a channel token was filed under a provider row: {sorted(stores)}"
    )


def test_vault_set_tells_the_assistant_the_channel_reader_will_find_it(stores):
    """``readable_as`` attested the provider row was readable by the Slack
    variable while the Slack reader could not see it. The dual has to agree
    with the reader or it is a third tool reporting success."""
    from robothor.vault.naming import env_names_for_vault_key

    assert "ROBOTHOR_SLACK_BOT_TOKEN" in env_names_for_vault_key("channels/slack/bot_token")
    assert "ROBOTHOR_SLACK_BOT_TOKEN" not in env_names_for_vault_key(
        "providers/robothor_slack_bot/api_key"
    )


@pytest.mark.parametrize(
    ("env", "key"),
    [
        ("ROBOTHOR_TELEGRAM_BOT_TOKEN", "channels/telegram/bot_token"),
        ("ROBOTHOR_EMAIL_SMTP_PASSWORD", "channels/email/smtp_password"),
        ("ROBOTHOR_TWILIO_AUTH_TOKEN", "channels/twilio/auth_token"),
        ("ROBOTHOR_TEAMS_APP_PASSWORD", "channels/teams/app_password"),
    ],
)
def test_every_declared_channel_secret_round_trips(stores, monkeypatch, capsys, env, key):
    """Telegram's key is the one the setup wizard writes
    (``crm/bridge/routers/setup.py``), so this is not a convention the naming
    module may choose — it is where the value already is."""
    from robothor import secrets as secrets_module
    from robothor.secrets import resolve_secret

    monkeypatch.setenv(env, "FAKE-channel-value-0000")
    _migrate(env)
    capsys.readouterr()
    assert key in stores, f"{env} was not filed at {key}: {sorted(stores)}"

    monkeypatch.delenv(env, raising=False)
    secrets_module.reset_secret_cache()
    assert resolve_secret(env, vault_key=key).value == "FAKE-channel-value-0000"
    assert resolve_secret(env).value == "FAKE-channel-value-0000", (
        "the reader that does NOT name the key cannot find it either"
    )

    # And the REAL reader, where one exists. Asserting on `resolve_secret`
    # alone is what let N2 through a whole round: this file's docstring said it
    # drove the reader, and it did not, so the Telegram token looked migrated
    # while `EngineConfig.from_env()` saw nothing.
    if env == "ROBOTHOR_TELEGRAM_BOT_TOKEN":
        from robothor.engine.config import EngineConfig
        from robothor.settings import reset_settings

        reset_settings()
        assert EngineConfig.from_env().bot_token == "FAKE-channel-value-0000", (
            "the daemon starts the Telegram channel from this value; after the "
            "runbook's shrink it would have been empty"
        )

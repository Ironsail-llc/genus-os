"""Bootstrap or application — derived from the settings model, never listed here.

The classification decides precedence, what ``genus secrets migrate`` refuses
to copy, and what an agent may be granted into an exec environment. A
hand-maintained list of names would drift from the model the moment somebody
added a setting, which is the defect ``hardcoded-names-drift`` is about: three
PRs for one bug, because a list of names was maintained beside the thing it
described.

So the source of truth is the declaration itself -- ``declare(...,
bootstrap=True)`` in :mod:`robothor.settings.model` -- plus exactly one
structural rule for names no settings field can carry: the variables that say
where the vault's own master key lives, and the SOPS age key that decrypts the
file the environment came from. Looking those up in the vault would be
circular.
"""

from __future__ import annotations

import pytest

from robothor.secrets.classification import (
    BOOTSTRAP_PREFIXES,
    bootstrap_names,
    is_bootstrap,
    is_declared_secret,
    non_secret_env_names,
)


@pytest.mark.parametrize(
    "name",
    [
        "ROBOTHOR_DB_PASSWORD",  # the vault's rows live in this database
        "ROBOTHOR_TEST_DB_DSN",
        "ROBOTHOR_TEST_ADMIN_DSN",
        "ROBOTHOR_REDIS_PASSWORD",
        "GENUS_AUTH_SIGNING_KEY",  # rotating it signs every session out
        "GENUS_BRIDGE_SSO_SECRET",
        "AUTH_SECRET",
        "ROBOTHOR_INTENT_HMAC_SECRET",
        "ROBOTHOR_NATS_PASSWORD",
        "ROBOTHOR_NATS_URL",
    ],
)
def test_the_credentials_that_bring_the_instance_up_are_bootstrap(name):
    assert is_bootstrap(name), f"{name} must keep environment-first precedence"
    assert name in bootstrap_names()


@pytest.mark.parametrize(
    "name",
    [
        "ROBOTHOR_VAULT_MASTER_KEY",
        "ROBOTHOR_VAULT_KEY_FILE",
        "SOPS_AGE_KEY_FILE",
    ],
)
def test_the_variables_that_open_the_vault_are_bootstrap_structurally(name):
    """No settings field carries these, and a vault-first lookup for them would
    ask the vault where its own key is."""
    assert is_bootstrap(name)
    assert name.startswith(BOOTSTRAP_PREFIXES)


@pytest.mark.parametrize(
    "name",
    [
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "BRAVE_API_KEY",
        "ROBOTHOR_TELEGRAM_BOT_TOKEN",
        "ROBOTHOR_SLACK_BOT_TOKEN",
        "ROBOTHOR_EMAIL_SMTP_PASSWORD",
        "ROBOTHOR_ALERT_WEBHOOK_URL",
    ],
)
def test_third_party_credentials_are_application_secrets(name):
    assert not is_bootstrap(name), f"{name} must be vault-first: the assistant rotates it"


def test_an_undeclared_name_is_an_application_secret():
    """The safe default. An assistant handed a credential for an integration
    nobody declared must still be able to keep it in the vault and have the
    vault win; the alternative -- unknown means bootstrap -- would silently
    restore env-first precedence for exactly the credentials this exists for.
    """
    assert not is_bootstrap("SOME_NEW_VENDOR_API_KEY")


def test_the_bootstrap_set_is_derived_from_the_model_not_written_down():
    """Marking a field bootstrap in the settings model is the only way in."""
    from robothor.settings.registry import field_index

    declared = {
        record["env"]
        for record in field_index().values()
        if record.get("bootstrap") and record["env"] in bootstrap_names()
    }
    assert "ROBOTHOR_DB_PASSWORD" in declared
    # Every declared bootstrap name reaches the set, and nothing else declared
    # does.
    for record in field_index().values():
        assert (record["env"] in bootstrap_names()) == bool(record.get("bootstrap"))


def test_every_bootstrap_field_is_also_a_secret():
    """Bootstrap is a statement about a CREDENTIAL's precedence.

    Marking a non-credential bootstrap would quietly exempt an ordinary
    setting from the vault without anyone meaning to.
    """
    from robothor.settings.registry import field_index

    for record in field_index().values():
        if record.get("bootstrap"):
            assert record["secret"], f"{record['env']} is bootstrap but not secret"


def test_declared_secrets_are_recognised_as_secret():
    assert is_declared_secret("ROBOTHOR_TELEGRAM_BOT_TOKEN")
    assert is_declared_secret("ROBOTHOR_DB_PASSWORD")
    assert not is_declared_secret("ROBOTHOR_DB_HOST")


def test_non_secret_names_exclude_every_declared_secret():
    """The exec allowlist is built from this set, so a secret leaking into it
    is a credential in every agent's shell."""
    from robothor.settings.registry import field_index

    safe = non_secret_env_names()
    for record in field_index().values():
        if record["secret"]:
            assert record["env"] not in safe, (
                f"{record['env']} is a secret and reached the allowlist"
            )
    assert "ROBOTHOR_DB_HOST" in safe
    assert "ROBOTHOR_WORKSPACE" in safe

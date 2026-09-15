"""Every spelling an assistant might store a credential under must be readable.

The 2026-09-15 incident, recurring with the tools attesting success. The
assistant is handed a GitHub token and stores it at ``providers/github/api_key``
— the spelling the vault handler's own docstring, the Helm provider page and
the first-run wizard all use. ``vault_get`` answers ``configured, source=vault``.
``vault_test`` dials it and says ``ok``. And every READER —
``resolve_secret("GITHUB_TOKEN")``, ``github_api._get_token()``, a
``secrets: [GITHUB_TOKEN]`` exec grant, the shadow check — keeps serving the
dead environment value, because ``vault_keys_for_env_name("GITHUB_TOKEN")``
offered only the literal ``github_token``.

Three tools reporting a rotation that did not happen is worse than the original
bug, so the mapping is now TOTAL for the shapes in use and is the only mapping
anything consults, in both directions:

    vault_keys_for_env_name(env)  -> which rows can answer for this variable
    env_names_for_vault_key(key)  -> which variables will find this row

``env_names_for_vault_key`` is what lets ``vault_set`` tell the model, at write
time, which readers its key will reach — the check that would have caught this
the moment it happened rather than at the next outage.
"""

from __future__ import annotations

import pytest

from robothor.vault.naming import env_names_for_vault_key, vault_keys_for_env_name

#: ``(environment name, a vault key a person or an assistant would plausibly
#: store it under)``. Every pair must round-trip in both directions.
SPELLINGS = [
    # Provider slots — what the wizard, the Helm page and key_pool write.
    ("OPENROUTER_API_KEY", "providers/openrouter/api_key"),
    ("OPENAI_API_KEY", "providers/openai/api_key"),
    ("ANTHROPIC_API_KEY", "providers/anthropic/api_key"),
    ("BRAVE_API_KEY", "providers/brave/api_key"),
    ("OPENROUTER_API_KEY_2", "providers/openrouter/api_key_2"),
    # Tokens. The shape the incident was about: nothing called these
    # "<X>_API_KEY", so nothing mapped them to a provider row.
    ("GITHUB_TOKEN", "providers/github/api_key"),
    ("GH_TOKEN", "providers/github/api_key"),
    ("JIRA_API_TOKEN", "providers/jira/api_key"),
    # Channel fields.
    ("ROBOTHOR_SLACK_BOT_TOKEN", "channels/slack/bot_token"),
    ("ROBOTHOR_TELEGRAM_BOT_TOKEN", "channels/telegram/bot_token"),
    ("ROBOTHOR_EMAIL_SMTP_PASSWORD", "channels/email/smtp_password"),
]


@pytest.mark.parametrize(("env", "key"), SPELLINGS)
def test_a_reader_of_the_variable_finds_the_row(env, key):
    assert key in vault_keys_for_env_name(env), (
        f"a credential stored at {key!r} is invisible to a reader of {env!r} — "
        "the incident, with the tools reporting success"
    )


@pytest.mark.parametrize(("env", "key"), SPELLINGS)
def test_the_row_knows_which_variable_will_find_it(env, key):
    assert env in env_names_for_vault_key(key), (
        f"{key!r} does not report {env!r} as a reader, so vault_set cannot warn "
        "an assistant that it has written somewhere nothing looks"
    )


@pytest.mark.parametrize(("env", "_key"), SPELLINGS)
def test_the_literal_lower_cased_spelling_always_works_too(env, _key):
    """``env_name()`` upper-cases and swaps ``/`` for ``_``, so the literal
    lower-cased name always exports back to the variable. It must stay a
    candidate: it is what ``genus secrets migrate`` used to write, and rows
    written by an earlier release are still out there."""
    assert env.lower() in vault_keys_for_env_name(env)


def test_the_canonical_candidate_comes_first():
    """``migrate`` and ``vault_set`` write ``candidates[0]``, so the order is
    what decides where a credential lands. The richer spelling wins, because
    that is the one ``key_pool`` and the Helm provider page already read."""
    assert vault_keys_for_env_name("OPENROUTER_API_KEY")[0] == "providers/openrouter/api_key"
    assert vault_keys_for_env_name("GITHUB_TOKEN")[0] == "providers/github/api_key"


def test_a_name_with_no_richer_spelling_still_gets_one_candidate():
    assert vault_keys_for_env_name("SOME_NEW_VENDOR_CREDENTIAL") == ("some_new_vendor_credential",)


def test_a_vault_key_nothing_can_read_reports_no_readers():
    """The warning ``vault_set`` needs: this row is real, encrypted and stored,
    and no reader in the platform will ever look at it."""
    assert env_names_for_vault_key("scratch/notes/2026") == ()


def test_padding_and_case_do_not_create_a_second_spelling():
    """``export_secrets`` upper-cases the whole key, so two rows differing only
    in case export to ONE variable and the second silently shadows the first
    depending on row order — the defect ``validate_component`` exists for."""
    assert vault_keys_for_env_name("  GITHUB_TOKEN  ") == vault_keys_for_env_name("GITHUB_TOKEN")
    assert vault_keys_for_env_name("github_token") == vault_keys_for_env_name("GITHUB_TOKEN")


# ── R2: a channel variable is never a provider row ───────────────────────────

#: Every declared channel secret, with the key its READER actually looks under.
#: `slack_credentials` passes `vault_key=channels/slack/bot_token` explicitly,
#: and the setup wizard writes `channels/telegram/bot_token`, so these are not a
#: convention this module is free to choose — they are where the values are.
CHANNEL_SPELLINGS = [
    ("ROBOTHOR_SLACK_BOT_TOKEN", "channels/slack/bot_token"),
    ("ROBOTHOR_SLACK_APP_TOKEN", "channels/slack/app_token"),
    ("ROBOTHOR_TELEGRAM_BOT_TOKEN", "channels/telegram/bot_token"),
    ("ROBOTHOR_EMAIL_SMTP_PASSWORD", "channels/email/smtp_password"),
    ("ROBOTHOR_TWILIO_AUTH_TOKEN", "channels/twilio/auth_token"),
    ("ROBOTHOR_TEAMS_APP_PASSWORD", "channels/teams/app_password"),
]


@pytest.mark.parametrize(("env", "key"), CHANNEL_SPELLINGS)
def test_a_channel_variable_canonicalises_to_its_channel_row(env, key):
    """The canonical candidate is what ``migrate`` and ``vault_set`` write.

    ``ROBOTHOR_SLACK_BOT_TOKEN`` matched the ``<VENDOR>_TOKEN`` rule first and
    canonicalised to ``providers/robothor_slack_bot/api_key`` — a row the Slack
    daemon's explicit-key reader never looks at. So ``migrate`` filed the token
    where nothing reads it, ``genus secrets status`` and ``readable_as``
    attested it was served from the vault, and after the runbook's "delete the
    migrated entries" the Slack daemon had no token at all.
    """
    assert vault_keys_for_env_name(env)[0] == key


@pytest.mark.parametrize(("env", "_key"), CHANNEL_SPELLINGS)
def test_a_channel_variable_offers_no_provider_candidate(env, _key):
    """Exclusive, not merely first. A provider candidate anywhere in the list is
    a row a later reader may find and serve, and ``ROBOTHOR_*`` is a platform
    prefix — it is never a vendor name."""
    offered = vault_keys_for_env_name(env)
    assert not [k for k in offered if k.startswith("providers/")], (
        f"{env} still offers a provider row: {offered}"
    )


@pytest.mark.parametrize(("env", "key"), CHANNEL_SPELLINGS)
def test_the_channel_row_claims_its_variable_back(env, key):
    assert env in env_names_for_vault_key(key)


def test_a_provider_row_does_not_claim_a_channel_variable():
    """The dual has to agree, or ``vault_set``'s ``readable_as`` tells an
    assistant that a row the channel reader cannot see is readable by it."""
    claimed = env_names_for_vault_key("providers/robothor_slack_bot/api_key")
    assert "ROBOTHOR_SLACK_BOT_TOKEN" not in claimed


def test_a_real_vendor_token_still_gets_its_provider_row():
    """The channel rule must not eat the shape C4 was about."""
    assert vault_keys_for_env_name("GITHUB_TOKEN")[0] == "providers/github/api_key"
    assert vault_keys_for_env_name("JIRA_API_TOKEN")[0] == "providers/jira/api_key"

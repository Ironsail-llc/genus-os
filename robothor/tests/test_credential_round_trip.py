"""The assistant stores a token; every reader must then serve it. End to end.

This is review finding C4, as the probe that found it. The assistant is handed
a GitHub token and stores it under the spelling the vault handler's docstring,
the first-run wizard and the Helm provider page all use —
``providers/github/api_key``. Before this test existed:

    resolve_secret('GITHUB_TOKEN')              -> env (the STALE one)
    github_api._get_token() serves fresh         -> False
    a `secrets: [GITHUB_TOKEN]` grant is fresh   -> False
    vault_get(providers/github/api_key)          -> configured, source=vault
    the shadow check                             -> no shadow

...which is the 2026-09-15 incident recurring, now with three tools attesting
that the rotation worked. A rotation nobody can see fail is worse than one that
fails loudly.

So the table below is every spelling in use, and each one is driven through
EVERY reader, not through the mapping function. A test that asserted on
``vault_keys_for_env_name`` alone would pass with the readers still broken.
"""

from __future__ import annotations

import pytest

STALE = "ghp_FAKE0000_the_dead_one_in_the_environment"
FRESH = "ghp_FAKE1111_the_one_the_assistant_just_stored"

#: ``(environment name, the vault key an assistant plausibly writes)``.
SPELLINGS = [
    ("GITHUB_TOKEN", "providers/github/api_key"),
    ("GITHUB_TOKEN", "github_token"),
    ("GH_TOKEN", "providers/github/api_key"),
    ("OPENROUTER_API_KEY", "providers/openrouter/api_key"),
    ("OPENROUTER_API_KEY", "openrouter_api_key"),
    ("ROBOTHOR_SLACK_BOT_TOKEN", "channels/slack/bot_token"),
]


@pytest.fixture
def stores(monkeypatch):
    """A vault double and a stale environment, both entirely fake."""
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


@pytest.mark.parametrize(("env_name", "vault_key"), SPELLINGS)
def test_the_accessor_serves_the_stored_value_over_the_stale_environment(
    stores, monkeypatch, env_name, vault_key
):
    from robothor.secrets import resolve_secret

    monkeypatch.setenv(env_name, STALE)
    stores[vault_key] = FRESH
    assert resolve_secret(env_name) == (FRESH, "vault"), (
        f"a credential stored at {vault_key!r} is invisible to a reader of "
        f"{env_name!r} — the incident, with vault_get reporting success"
    )


def test_the_github_tool_serves_the_stored_value(stores, monkeypatch):
    from robothor.engine.tools.handlers.github_api import _get_token

    monkeypatch.setenv("GITHUB_TOKEN", STALE)
    stores["providers/github/api_key"] = FRESH
    assert _get_token() == FRESH


def test_an_exec_grant_injects_the_stored_value(stores, monkeypatch):
    from robothor.engine.exec_env import MODE_ENFORCE, build_exec_env

    monkeypatch.setenv("GITHUB_TOKEN", STALE)
    stores["providers/github/api_key"] = FRESH
    built = build_exec_env(
        agent_id="devops",
        mode=MODE_ENFORCE,
        base={"PATH": "/usr/bin", "GITHUB_TOKEN": STALE},
        grants=("GITHUB_TOKEN",),
    )
    assert built.env["GITHUB_TOKEN"] == FRESH
    assert built.unresolved == ()


def test_the_shadow_check_sees_the_disagreement(stores, monkeypatch):
    """I1: the shadow check read only ``export_env()``, so the most common
    class of credential — a provider slot — could never be reported."""
    from robothor.secrets.status import status_for_name

    monkeypatch.setenv("OPENROUTER_API_KEY", STALE)
    stores["providers/openrouter/api_key"] = FRESH
    row = status_for_name("OPENROUTER_API_KEY")
    assert row.in_env and row.in_vault
    assert row.shadowed, "a stale environment value beside a live provider row is THE shadow"
    assert row.source == "vault"


def test_the_status_table_reports_one_row_per_credential(stores, monkeypatch):
    """I1: a vault row and the variable it answers for are one credential.

    Two rows that never meet is how the table showed ``OPENROUTER_API_KEY
    env=yes vault=no`` beside ``PROVIDERS_OPENROUTER_API_KEY env=no vault=yes``
    and detected no shadow between them.
    """
    from robothor.secrets.status import status_table

    monkeypatch.setenv("OPENROUTER_API_KEY", STALE)
    stores["providers/openrouter/api_key"] = FRESH
    names = [row.name for row in status_table()]
    assert "OPENROUTER_API_KEY" in names
    assert "PROVIDERS_OPENROUTER_API_KEY" not in names, (
        "the vault row was emitted as its own credential instead of being "
        "merged into the variable that reads it"
    )
    row = next(row for row in status_table() if row.name == "OPENROUTER_API_KEY")
    assert row.in_vault and row.shadowed


def test_migrate_does_not_re_plan_a_credential_it_already_moved(stores, monkeypatch, capsys):
    """I1's other half: ``migrate`` compared against the same blind lookup, so
    a provider key was reported as stored on every single re-run."""
    import argparse

    from robothor.cli.secrets_cmd import cmd_secrets

    monkeypatch.setenv("OPENROUTER_API_KEY", FRESH)
    args = argparse.Namespace(
        secrets_command="migrate", from_env=True, dry_run=False, only=None, tenant=None
    )
    cmd_secrets(args)
    capsys.readouterr()
    cmd_secrets(args)
    second = capsys.readouterr().out
    assert "already" in second.lower()
    assert "stored  OPENROUTER_API_KEY" not in second

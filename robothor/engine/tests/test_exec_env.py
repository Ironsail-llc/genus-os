"""Every exec-capable agent could read all ~50 of the instance's credentials.

``filesystem._exec`` called ``subprocess.run`` with no ``env=``, and
``Sandbox.exec`` built one with ``os.environ.copy()``. The engine's process
environment is loaded at boot from a root-owned SOPS file and carries every
channel token, provider key and database password the instance owns, so the
child of an ``exec`` inherited all of them — and so did the child of a
SUB-agent's ``exec``, which is worse: a sub-agent is spawned to do one narrow
thing and had the whole credential set in its shell.

The child environment is now built from an ALLOWLIST. The allowlist is derived,
not written down: the process essentials plus the ``ROBOTHOR_*``/``GENUS_*``
names the settings model marks NON-secret. An agent that genuinely needs a
credential says so in its own manifest (``secrets: [NAMES]``) and gets exactly
those names, resolved through the accessor so the vault wins — and a sub-agent
inherits none of them, because the grant is read from the manifest of the agent
whose id is on the tool context, which for a spawned child is the child's.
"""

from __future__ import annotations

import logging

import pytest

from robothor.engine.exec_env import (
    ALWAYS_ALLOWED,
    MODE_ENFORCE,
    MODE_OBSERVE,
    MODE_OFF,
    build_exec_env,
    exec_env_mode,
)

#: Obviously fake, and distinctive enough that a substring search cannot match
#: by accident.
FAKE_GITHUB = "ghp_FAKE0000000000000000000000000000000000"
FAKE_SLACK = "xoxb-FAKE-0000-0000-fakefakefakefake"
FAKE_DB = "fake-db-password-0000"


@pytest.fixture
def boxlike_env():
    """A process environment shaped like the engine's on a real instance."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "xterm",
        "TZ": "UTC",
        "TMPDIR": "/tmp",  # noqa: S108 - a fixture value, not a path this code writes
        "ROBOTHOR_WORKSPACE": "/workspace",
        "ROBOTHOR_AGENT_ID": "researcher",
        "ROBOTHOR_TENANT_ID": "acme",
        "ROBOTHOR_DB_HOST": "localhost",
        # ...and the credentials.
        "GITHUB_TOKEN": FAKE_GITHUB,
        "GH_TOKEN": FAKE_GITHUB,
        "ROBOTHOR_SLACK_BOT_TOKEN": FAKE_SLACK,
        "ROBOTHOR_DB_PASSWORD": FAKE_DB,
        "OPENROUTER_API_KEY": "sk-or-FAKE000000000000000000",
        "AWS_SECRET_ACCESS_KEY": "FAKE0000000000000000",
        "SOME_VENDOR_PASSWORD": "fake-vendor-0000",
    }


SECRET_VALUES = (
    FAKE_GITHUB,
    FAKE_SLACK,
    FAKE_DB,
    "sk-or-FAKE000000000000000000",
    "FAKE0000000000000000",
    "fake-vendor-0000",
)


def _no_credential_survives(env: dict[str, str]) -> None:
    leaked = [name for name, value in env.items() if value in SECRET_VALUES]
    assert not leaked, f"credentials reached the child environment: {leaked}"


# ── the allowlist ────────────────────────────────────────────────────────────


def test_enforce_keeps_the_process_essentials(boxlike_env):
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=boxlike_env)
    for name in ("PATH", "HOME", "LANG", "TERM", "TZ", "TMPDIR", "ROBOTHOR_WORKSPACE"):
        assert built.env.get(name) == boxlike_env[name], f"{name} was dropped"
    assert built.env["LC_ALL"] == "en_US.UTF-8", "LC_* is locale, not a credential"
    assert set(ALWAYS_ALLOWED) <= set(boxlike_env) | set(built.env)


def test_enforce_keeps_non_secret_platform_settings(boxlike_env):
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=boxlike_env)
    assert built.env["ROBOTHOR_TENANT_ID"] == "acme"
    assert built.env["ROBOTHOR_DB_HOST"] == "localhost"


def test_enforce_drops_every_credential(boxlike_env):
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=boxlike_env)
    _no_credential_survives(built.env)
    for name in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ROBOTHOR_SLACK_BOT_TOKEN",
        "ROBOTHOR_DB_PASSWORD",
        "OPENROUTER_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "SOME_VENDOR_PASSWORD",
    ):
        assert name not in built.env, f"{name} survived the scrub"
        assert name in built.withheld, f"{name} was dropped without being reported"


def test_gh_finds_no_token_and_falls_back_to_its_own_login(boxlike_env):
    """Named explicitly because it changes what an operator sees.

    With neither ``GH_TOKEN`` nor ``GITHUB_TOKEN`` in the child environment,
    ``gh`` uses its own login file instead of the engine's credential — which
    is the point: the agent acts as whoever ``gh auth login`` authorised, not
    as the instance.
    """
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=boxlike_env)
    assert "GH_TOKEN" not in built.env
    assert "GITHUB_TOKEN" not in built.env


def test_an_undeclared_platform_variable_is_dropped(boxlike_env):
    """The allowlist is an allowlist. A ``ROBOTHOR_*`` name nobody declared is
    not known to be safe, so it does not travel."""
    base = dict(boxlike_env, ROBOTHOR_MYSTERY_THING="whatever")
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=base)
    assert "ROBOTHOR_MYSTERY_THING" not in built.env


def test_a_credential_shaped_value_in_a_non_secret_setting_is_dropped(boxlike_env):
    """The hostile move: paste a token into a setting the model calls harmless.

    A declared non-secret name is allowed THROUGH the name gate and then
    checked on its value, because an operator who pastes a token into
    ``ROBOTHOR_LAST_RESORT_MODEL`` has created a credential the name gate has
    no way to see.
    """
    base = dict(boxlike_env, ROBOTHOR_LAST_RESORT_MODEL=FAKE_GITHUB)
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=base)
    assert "ROBOTHOR_LAST_RESORT_MODEL" not in built.env
    assert "ROBOTHOR_LAST_RESORT_MODEL" in built.withheld
    _no_credential_survives(built.env)


# ── the ladder ───────────────────────────────────────────────────────────────


def test_off_changes_nothing(boxlike_env):
    """An existing install must not lose a credential its agents were using
    before the operator has seen what would be taken."""
    built = build_exec_env(agent_id="researcher", mode=MODE_OFF, base=boxlike_env)
    assert built.env == boxlike_env
    assert built.withheld == (), (
        "off means the control is off: reporting names nothing acted on would "
        "read, in the doctor and the status table, as though something had"
    )


def test_observe_keeps_everything_and_names_what_enforce_would_take(boxlike_env, caplog):
    built = build_exec_env(agent_id="researcher", mode=MODE_OBSERVE, base=boxlike_env)
    assert built.env == boxlike_env, "observe must not change behaviour"
    assert "GITHUB_TOKEN" in built.withheld
    assert "ROBOTHOR_DB_PASSWORD" in built.withheld


def test_observe_logs_the_names_for_the_agent_but_never_a_value(boxlike_env, caplog):
    with caplog.at_level(logging.INFO, logger="robothor.engine.exec_env"):
        build_exec_env(agent_id="researcher", mode=MODE_OBSERVE, base=boxlike_env)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "researcher" in text, "an observation nobody can attribute is not evidence"
    assert "GITHUB_TOKEN" in text
    for value in SECRET_VALUES:
        assert value not in text, "the observe rung printed a credential"


def test_the_mode_defaults_to_observe_for_an_install_that_never_chose(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_EXEC_ENV_MODE", raising=False)
    assert exec_env_mode() == MODE_OBSERVE


def test_an_unknown_mode_is_observe_not_off(monkeypatch):
    """A typo must not silently disable the control. ``observe`` is the rung
    that changes nothing and still reports, so it is the safe fallback."""
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enfroce")
    assert exec_env_mode() == MODE_OBSERVE


# ── per-agent grants ─────────────────────────────────────────────────────────


@pytest.fixture
def granted_vault(monkeypatch):
    from robothor import secrets as secrets_module
    from robothor import vault

    rows = {"GITHUB_TOKEN": "ghp_FAKE2222_from_the_vault"}
    monkeypatch.setattr(vault, "get", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(vault, "export_env", lambda **kw: dict(rows))
    secrets_module.reset_vault_availability()
    yield rows
    secrets_module.reset_vault_availability()


def test_a_granted_name_is_injected_from_the_accessor(boxlike_env, granted_vault):
    built = build_exec_env(
        agent_id="devops",
        mode=MODE_ENFORCE,
        base=boxlike_env,
        grants=("GITHUB_TOKEN",),
    )
    assert built.env["GITHUB_TOKEN"] == "ghp_FAKE2222_from_the_vault", (
        "the grant must come from the accessor (vault first), not from the "
        "environment the scrub just rejected"
    )
    assert built.granted == ("GITHUB_TOKEN",)


def test_a_grant_is_the_only_credential_that_travels(boxlike_env, granted_vault):
    built = build_exec_env(
        agent_id="devops",
        mode=MODE_ENFORCE,
        base=boxlike_env,
        grants=("GITHUB_TOKEN",),
    )
    assert "ROBOTHOR_SLACK_BOT_TOKEN" not in built.env
    assert "OPENROUTER_API_KEY" not in built.env
    assert "ROBOTHOR_DB_PASSWORD" not in built.env


def test_an_agent_with_no_grant_gets_no_credential(boxlike_env, granted_vault):
    """A sub-agent's case, stated as the general rule: grants are not ambient.

    ``build_exec_env`` is told the grants of the agent whose id is on the tool
    context. A spawned sub-agent's context carries the CHILD's id, so the
    child's own manifest is what is read, and a child that names nothing gets
    nothing regardless of what its parent holds.
    """
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=boxlike_env)
    assert built.granted == ()
    _no_credential_survives(built.env)


def test_a_bootstrap_name_can_never_be_granted(boxlike_env, granted_vault):
    """Handing an agent the database password would hand it every tenant's
    data and the vault's own rows, grant or no grant."""
    built = build_exec_env(
        agent_id="devops",
        mode=MODE_ENFORCE,
        base=boxlike_env,
        grants=("ROBOTHOR_DB_PASSWORD",),
    )
    assert "ROBOTHOR_DB_PASSWORD" not in built.env
    assert "ROBOTHOR_DB_PASSWORD" in built.refused
    assert built.granted == ()


def test_a_grant_the_accessor_cannot_resolve_is_loud(boxlike_env, granted_vault):
    """A silent absence is the failure this codebase keeps re-learning: the
    agent runs the command, the command fails for an unrelated-looking reason,
    and nothing says the credential was never there."""
    built = build_exec_env(
        agent_id="devops",
        mode=MODE_ENFORCE,
        base=boxlike_env,
        grants=("BRAVE_API_KEY",),
    )
    assert "BRAVE_API_KEY" not in built.env
    assert "BRAVE_API_KEY" in built.unresolved
    assert built.note, "an unresolved grant must produce a note the model reads"
    assert "BRAVE_API_KEY" in built.note


def test_the_note_never_contains_a_value(boxlike_env, granted_vault):
    built = build_exec_env(
        agent_id="devops",
        mode=MODE_ENFORCE,
        base=boxlike_env,
        grants=("GITHUB_TOKEN", "BRAVE_API_KEY", "ROBOTHOR_DB_PASSWORD"),
    )
    note = built.note or ""
    for value in (*SECRET_VALUES, "ghp_FAKE2222_from_the_vault"):
        assert value not in note


def test_grants_are_honoured_even_at_off(boxlike_env, granted_vault):
    """The ladder governs what is TAKEN AWAY. A grant adds, so it applies on
    every rung — otherwise promoting the flag would be the thing that first
    gives an agent a credential, and the rung would not be a no-op."""
    built = build_exec_env(
        agent_id="devops", mode=MODE_OFF, base=boxlike_env, grants=("GITHUB_TOKEN",)
    )
    assert built.env["GITHUB_TOKEN"] == "ghp_FAKE2222_from_the_vault"

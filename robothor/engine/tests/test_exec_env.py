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


def test_an_ungranted_agent_does_not_inherit_the_operators_gh_login(boxlike_env):
    """Review finding I6, and the first cut had this exactly backwards.

    Taking ``GH_TOKEN`` out of the child does not make ``gh`` fail — it makes
    it fall back to ``~/.config/gh/hosts.yml``. The docs called that a feature
    ("the agent acts as whoever ran `gh auth login`"), which is a WIDER
    identity than the instance's token, not a narrower one: before this change
    the dead ``GH_TOKEN`` made ``gh`` fail for every agent, and after it every
    ungranted exec agent — sub-agents included — would have been running as the
    operator personally.

    So an ungranted agent gets an empty ``GH_CONFIG_DIR``: logged out, plainly.
    """
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=boxlike_env)
    assert "GH_TOKEN" not in built.env
    assert "GITHUB_TOKEN" not in built.env
    assert built.env.get("GH_CONFIG_DIR"), "gh was left to find the operator's own login file"


def test_a_granted_agent_acts_as_the_instance(boxlike_env, granted_vault):
    """Granted ``GITHUB_TOKEN``, ``gh`` uses it and needs no config redirect —
    the agent acts as the instance, which is the identity an operator chose."""
    built = build_exec_env(
        agent_id="devops", mode=MODE_ENFORCE, base=boxlike_env, grants=("GITHUB_TOKEN",)
    )
    assert built.env["GITHUB_TOKEN"] == "ghp_FAKE2222_from_the_vault"
    assert "GH_CONFIG_DIR" not in built.env


def test_the_gh_redirect_is_not_applied_below_enforce(boxlike_env):
    """``observe`` changes nothing. A config redirect is a behaviour change,
    so it belongs on the rung that makes behaviour changes."""
    built = build_exec_env(agent_id="researcher", mode=MODE_OBSERVE, base=boxlike_env)
    assert "GH_CONFIG_DIR" not in built.env


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
    import robothor.engine.exec_env as exec_env

    exec_env._observed_at.clear()
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

    # Keyed the way the accessor asks — by VAULT key, through the one mapping.
    # Keying by environment name modelled an accessor that decrypts every row
    # to answer about one, which is the implementation this no longer has.
    rows = {"providers/github/api_key": "ghp_FAKE2222_from_the_vault"}
    monkeypatch.setattr(vault, "get", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(
        vault, "export_env", lambda **kw: {k.upper().replace("/", "_"): v for k, v in rows.items()}
    )
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


# ── I4: a credential hiding in a setting nobody calls secret ─────────────────


def test_a_dsn_with_a_password_in_a_non_secret_setting_is_dropped(boxlike_env):
    """Review finding I4, and the hardest of the value cases.

    ``ROBOTHOR_DB_HOST`` is declared non-secret and accepts a full DSN — the
    settings package builds a connection pool from one. So an operator who
    configured the database that way has put a password into a variable the
    name gate is obliged to pass, and the prefix list cannot see it either: a
    DSN starts with ``postgresql://``, not with a vendor token prefix.
    """
    base = dict(boxlike_env, ROBOTHOR_DB_HOST="postgresql://alice:FAKEhunter2@db/genus")
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=base)
    assert "ROBOTHOR_DB_HOST" not in built.env
    assert "ROBOTHOR_DB_HOST" in built.withheld


def test_a_url_with_userinfo_anywhere_is_dropped(boxlike_env):
    base = dict(boxlike_env, ROBOTHOR_SEARXNG_URL="https://user:FAKEpw0000@search.example/x")
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=base)
    assert "ROBOTHOR_SEARXNG_URL" not in built.env


def test_a_url_without_userinfo_survives(boxlike_env):
    """The cost of a false positive here is an agent that cannot reach its own
    services, so the gate takes the userinfo run and nothing else."""
    base = dict(boxlike_env, ROBOTHOR_DB_HOST="postgresql://db.internal:5432/genus")
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=base)
    assert built.env["ROBOTHOR_DB_HOST"] == "postgresql://db.internal:5432/genus"


@pytest.mark.parametrize("name", ["TMPDIR", "HOME", "LC_PAPER", "LANG"])
def test_even_the_always_allowed_names_are_checked_on_their_value(boxlike_env, name):
    """``ALWAYS_ALLOWED`` and ``LC_*`` skipped the value gate entirely, so a
    credential parked in ``LC_PAPER`` or ``TMPDIR`` travelled untouched. The
    allowlist is about which names are STRUCTURALLY needed, not a promise about
    what somebody put in them."""
    base = dict(boxlike_env)
    base[name] = "postgresql://alice:FAKEhunter2@db/genus"
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base=base)
    assert built.env.get(name) != base[name], f"{name} carried a credential through"


def test_the_value_gate_is_the_redactor_not_a_second_opinion():
    """One matcher, not two. A shape the redactor learns about must reach the
    exec gate without anybody editing a second list."""
    from robothor.engine.exec_env import looks_like_a_credential_value
    from robothor.secrets.redaction import redact

    for value in (
        "ghp_FAKE0000aaaaaaaaaaaaaaaaaaaaaaaa",
        "postgresql://alice:FAKEpw@db/genus",
        "xoxb-FAKE-0000-0000-fakefakefake",
    ):
        assert looks_like_a_credential_value(value)
        assert redact(value) != value, (
            "the exec gate and the redactor disagree about this value, which "
            "means there are two matchers again"
        )


def test_the_observe_line_is_rate_limited_per_agent(boxlike_env, caplog):
    """~50 names on ~250 execs a day is not a count, it is a flood — and a
    flood is how the credential pool logged one outage 452 times while paging
    zero. One line per agent per hour is a count an operator can act on."""
    import robothor.engine.exec_env as exec_env

    exec_env._observed_at.clear()
    with caplog.at_level(logging.INFO, logger="robothor.engine.exec_env"):
        for _ in range(20):
            build_exec_env(agent_id="researcher", mode=MODE_OBSERVE, base=boxlike_env)
    lines = [r for r in caplog.records if "would lose" in r.getMessage()]
    assert len(lines) == 1, f"{len(lines)} observe lines for 20 execs by one agent"
    exec_env._observed_at.clear()


def test_a_second_agent_still_gets_its_own_line(boxlike_env, caplog):
    """Per AGENT, not global: the report is per-agent or it is not actionable."""
    import robothor.engine.exec_env as exec_env

    exec_env._observed_at.clear()
    with caplog.at_level(logging.INFO, logger="robothor.engine.exec_env"):
        build_exec_env(agent_id="researcher", mode=MODE_OBSERVE, base=boxlike_env)
        build_exec_env(agent_id="devops", mode=MODE_OBSERVE, base=boxlike_env)
    lines = [r for r in caplog.records if "would lose" in r.getMessage()]
    assert len(lines) == 2
    exec_env._observed_at.clear()

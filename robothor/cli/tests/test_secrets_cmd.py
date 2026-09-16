"""``genus secrets status`` and ``genus secrets migrate --from-env``.

Two commands for the two questions an operator has after the vault became the
store that wins.

*Which store is serving each credential, and are any of them disagreeing?* —
``status``. It is the same table the doctor's ``secrets.shadowed`` check and
the Helm Secrets page read, from the same function, because "the UI calls it
configured and the engine calls it missing" is a failure this repo has already
paid for once.

*How do I stop the environment being the place my credentials live?* —
``migrate --from-env``, which copies every APPLICATION credential the process
environment holds into the vault. It refuses bootstrap names outright: the
database password is what the vault's own rows live behind, and an operator who
moved it into the vault would have a box that cannot start and no obvious way
back.

Neither command ever prints a value. That is asserted here against a fixture
token distinctive enough that a substring search over the whole captured output
cannot match it by accident.
"""

from __future__ import annotations

import argparse

import pytest

FAKE_ENV_TOKEN = "ghp_FAKE0000_in_the_environment_only_0000"
FAKE_VAULT_TOKEN = "ghp_FAKE1111_in_the_vault_only_1111"
FAKE_DB_PASSWORD = "fake-bootstrap-db-password-0000"


@pytest.fixture
def stores(monkeypatch):
    """A process environment and a vault, both entirely fake."""
    from robothor import secrets as secrets_module
    from robothor import vault

    rows: dict[str, str] = {}

    monkeypatch.setattr(vault, "get", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(
        vault, "export_env", lambda **kw: {k.upper().replace("/", "_"): v for k, v in rows.items()}
    )
    monkeypatch.setattr(vault, "set", lambda key, value, **kw: rows.__setitem__(key, value))
    monkeypatch.setattr(vault, "list", lambda **kw: sorted(rows))
    secrets_module.reset_vault_availability()
    yield rows
    secrets_module.reset_vault_availability()


def _run(capsys, **kwargs) -> tuple[int, str]:
    from robothor.cli.secrets_cmd import cmd_secrets

    args = argparse.Namespace(**kwargs)
    code = cmd_secrets(args)
    return code, capsys.readouterr().out


# ── status ───────────────────────────────────────────────────────────────────


def test_status_names_the_credential_and_never_its_value(stores, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    code, out = _run(capsys, secrets_command="status", tenant=None)
    assert code == 0
    assert "GITHUB_TOKEN" in out
    assert FAKE_ENV_TOKEN not in out


def test_status_says_which_store_wins(stores, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    # Stored under the key `vault_keys_for_env_name("GITHUB_TOKEN")` chooses,
    # which is the whole point: a row written where the migration writes it is
    # a row the accessor finds.
    stores["github_token"] = FAKE_VAULT_TOKEN
    _, out = _run(capsys, secrets_command="status", tenant=None)
    line = next(line for line in out.splitlines() if "GITHUB_TOKEN" in line)
    assert "vault" in line, f"the table did not say which store is served: {line!r}"


def test_status_carries_a_fingerprint_so_two_boxes_can_be_compared(stores, monkeypatch, capsys):
    from robothor.secrets.fingerprint import fingerprint

    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    _, out = _run(capsys, secrets_command="status", tenant=None)
    assert fingerprint(FAKE_ENV_TOKEN) in out


def test_status_flags_a_shadow(stores, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    stores["github_token"] = FAKE_VAULT_TOKEN
    _, out = _run(capsys, secrets_command="status", tenant=None)
    assert "shadow" in out.lower()


# ── migrate ──────────────────────────────────────────────────────────────────


def test_a_dry_run_writes_nothing(stores, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    code, out = _run(
        capsys, secrets_command="migrate", from_env=True, dry_run=True, only=None, tenant=None
    )
    assert code == 0
    assert "GITHUB_TOKEN" in out
    assert stores == {}, "a dry run wrote to the vault"


def test_a_real_run_copies_the_credential_into_the_vault(stores, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    code, out = _run(
        capsys, secrets_command="migrate", from_env=True, dry_run=False, only=None, tenant=None
    )
    assert code == 0
    assert FAKE_ENV_TOKEN in stores.values()
    assert FAKE_ENV_TOKEN not in out, "the migration printed the credential it moved"


def test_migration_refuses_a_bootstrap_credential(stores, monkeypatch, capsys):
    """An operator who moved the database password into the vault would have a
    box that cannot start and no obvious way back: the vault's own rows live
    behind that password."""
    monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", FAKE_DB_PASSWORD)
    _, out = _run(
        capsys, secrets_command="migrate", from_env=True, dry_run=False, only=None, tenant=None
    )
    assert FAKE_DB_PASSWORD not in stores.values()
    assert "ROBOTHOR_DB_PASSWORD" in out
    assert "bootstrap" in out.lower()


def test_only_narrows_the_migration(stores, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    monkeypatch.setenv("ROBOTHOR_SLACK_BOT_TOKEN", "xoxb-FAKE-0000")
    _run(
        capsys,
        secrets_command="migrate",
        from_env=True,
        dry_run=False,
        only=["GITHUB_TOKEN"],
        tenant=None,
    )
    assert FAKE_ENV_TOKEN in stores.values()
    assert "xoxb-FAKE-0000" not in stores.values()


def test_a_name_explicitly_asked_for_and_refused_is_still_reported(stores, monkeypatch, capsys):
    """Silence on ``--only ROBOTHOR_DB_PASSWORD`` would read as success."""
    monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", FAKE_DB_PASSWORD)
    code, out = _run(
        capsys,
        secrets_command="migrate",
        from_env=True,
        dry_run=False,
        only=["ROBOTHOR_DB_PASSWORD"],
        tenant=None,
    )
    assert "ROBOTHOR_DB_PASSWORD" in out
    assert code != 0, "a refusal the operator explicitly asked for must not exit 0"


def test_a_value_already_identical_in_the_vault_is_skipped(stores, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    stores["github_token"] = FAKE_ENV_TOKEN
    _, out = _run(
        capsys, secrets_command="migrate", from_env=True, dry_run=False, only=None, tenant=None
    )
    assert "already" in out.lower() or "unchanged" in out.lower()


# ── R1: migrate must never revert a rotation ─────────────────────────────────


def test_migrate_never_overwrites_a_differing_vault_row(stores, monkeypatch, capsys):
    """Review R1, and it was the runbook's own step 3 doing it.

    The vault holds what the assistant rotated to; the environment holds the
    dead token the box booted with. `migrate` compared fingerprints, saw they
    differed, and WROTE — reverting every rotation the assistant had ever
    performed, and printing `stored` while it did.

    The precedence rule is "a vault row that exists beats the environment". A
    migration that inverts it is the incident, executed by the documented
    remediation for the incident.
    """
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    stores["providers/github/api_key"] = FAKE_VAULT_TOKEN

    code, out = _run(
        capsys, secrets_command="migrate", from_env=True, dry_run=False, only=None, tenant=None
    )

    assert stores["providers/github/api_key"] == FAKE_VAULT_TOKEN, (
        "the migration overwrote the vault's value with the stale environment copy"
    )
    assert code == 0
    assert "GITHUB_TOKEN" in out
    assert "conflict" in out.lower() or "shadow" in out.lower()


def test_the_conflict_line_names_both_fingerprints_and_neither_value(stores, monkeypatch, capsys):
    """An operator deciding which copy to keep needs to tell them apart."""
    from robothor.secrets.fingerprint import fingerprint

    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    stores["providers/github/api_key"] = FAKE_VAULT_TOKEN
    _, out = _run(
        capsys, secrets_command="migrate", from_env=True, dry_run=False, only=None, tenant=None
    )
    assert fingerprint(FAKE_ENV_TOKEN) in out
    assert fingerprint(FAKE_VAULT_TOKEN) in out
    assert FAKE_ENV_TOKEN not in out
    assert FAKE_VAULT_TOKEN not in out


def test_the_dry_run_reports_the_conflict_too(stores, monkeypatch, capsys):
    """A dry run that says `would store` and a real run that skips would send
    an operator looking for a bug in the real run."""
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    stores["providers/github/api_key"] = FAKE_VAULT_TOKEN
    _, out = _run(
        capsys, secrets_command="migrate", from_env=True, dry_run=True, only=None, tenant=None
    )
    assert "would store  GITHUB_TOKEN" not in out
    assert "conflict" in out.lower() or "shadow" in out.lower()


def test_overwrite_names_the_one_credential_it_may_replace(stores, monkeypatch, capsys):
    """The escape hatch is per NAME, never a blanket flag: an operator who
    means to revert one credential does not mean to revert all of them."""
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    monkeypatch.setenv("BRAVE_API_KEY", "brave-FAKE-env-0000")
    stores["providers/github/api_key"] = FAKE_VAULT_TOKEN
    stores["providers/brave/api_key"] = "brave-FAKE-vault-1111"

    _run(
        capsys,
        secrets_command="migrate",
        from_env=True,
        dry_run=False,
        only=None,
        tenant=None,
        overwrite=["GITHUB_TOKEN"],
    )
    assert stores["providers/github/api_key"] == FAKE_ENV_TOKEN, "the named row was not replaced"
    assert stores["providers/brave/api_key"] == "brave-FAKE-vault-1111", (
        "--overwrite GITHUB_TOKEN replaced a credential it did not name"
    )


def test_an_absent_vault_row_is_still_migrated_normally(stores, monkeypatch, capsys):
    """The conflict rule must not break the ordinary case, which is the whole
    point of the command: a credential the vault does not hold yet."""
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    _run(capsys, secrets_command="migrate", from_env=True, dry_run=False, only=None, tenant=None)
    assert FAKE_ENV_TOKEN in stores.values()


def test_a_conflicting_name_is_not_also_reported_as_unset(stores, monkeypatch, capsys):
    """Review N10: the tail loop checked planned/refused/unchanged and not
    conflicts, so `--only GITHUB_TOKEN` printed both `CONFLICT GITHUB_TOKEN`
    and `skipped GITHUB_TOKEN — not set in this environment`. Two contradictory
    lines for one name is how an operator stops trusting the output."""
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_ENV_TOKEN)
    stores["providers/github/api_key"] = FAKE_VAULT_TOKEN
    _, out = _run(
        capsys,
        secrets_command="migrate",
        from_env=True,
        dry_run=False,
        only=["GITHUB_TOKEN"],
        tenant=None,
        overwrite=None,
    )
    assert "CONFLICT GITHUB_TOKEN" in out
    assert "not set in this environment" not in out

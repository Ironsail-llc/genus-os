"""A value the operator hands the assistant beats the value the box booted with.

The incident, 2026-09-15: the operator handed the assistant a GitHub token and
expected it to be kept, used and rotated by the assistant. It could not be. The
engine's process environment carries the credentials decrypted at boot from a
root-owned SOPS file; the assistant can write the vault but the accessor read
the ENVIRONMENT FIRST, so an expired ``GH_TOKEN`` in the environment shadowed
the fresh vault row for as long as the unit stayed up -- and the assistant can
neither edit the SOPS file nor restart the unit, which is correct: it must
never need to.

So the chain is no longer one chain. It is two, chosen by what the credential
IS:

* **Bootstrap** -- the credentials that bring the instance up, including the
  ones the vault itself needs (the database password: the vault rows live in
  that database). Environment first. A vault-first lookup for these is either
  circular or a way to lock an operator out of their own box.
* **Application** -- every other credential: the third-party tokens an
  assistant is handed, uses, proves and rotates. Vault first. A row in the
  vault always beats the environment, because the vault is the store the
  operator and the assistant can both write and the environment is a snapshot
  of a file only root can edit.

Which one answered is reported as ``source`` so the doctor, the status table
and the Helm page can all say it out loud.
"""

from __future__ import annotations

import pytest

from robothor import secrets as secrets_module
from robothor.secrets import resolve_secret

#: Obviously fake. A real value must never appear in a fixture.
ENV_VALUE = "ghp_FAKE0000_from_the_environment"
VAULT_VALUE = "ghp_FAKE1111_from_the_vault"

#: Declared in the settings model with ``bootstrap=True``: the database
#: password, without which there is no vault to read.
BOOTSTRAP_NAME = "ROBOTHOR_DB_PASSWORD"

#: An application credential. Undeclared on purpose -- the tokens an assistant
#: is handed are not settings, and "undeclared" must mean "application", never
#: "bootstrap", or a new integration would silently get env-first precedence.
APPLICATION_NAME = "GITHUB_TOKEN"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (BOOTSTRAP_NAME, APPLICATION_NAME):
        monkeypatch.delenv(name, raising=False)
    secrets_module.reset_vault_availability()
    yield
    secrets_module.reset_vault_availability()


def _vault_holds(monkeypatch, mapping: dict[str, str]) -> None:
    from robothor import vault

    monkeypatch.setattr(vault, "get", lambda key, **kw: mapping.get(key))
    monkeypatch.setattr(vault, "export_env", lambda **kw: dict(mapping))


def _vault_down(monkeypatch) -> None:
    from robothor import vault

    def boom(*args, **kwargs):
        raise RuntimeError("vault unreadable")

    monkeypatch.setattr(vault, "get", boom)
    monkeypatch.setattr(vault, "export_env", boom)


# ── the table ────────────────────────────────────────────────────────────────

#: ``(name, in_env, in_vault, expected_value, expected_source)``.
PRECEDENCE = [
    # An application credential: the vault wins whenever it holds a row.
    (APPLICATION_NAME, True, True, VAULT_VALUE, "vault"),
    (APPLICATION_NAME, False, True, VAULT_VALUE, "vault"),
    (APPLICATION_NAME, True, False, ENV_VALUE, "env"),
    (APPLICATION_NAME, False, False, None, "missing"),
    # A bootstrap credential: the environment wins whenever it holds a value.
    (BOOTSTRAP_NAME, True, True, ENV_VALUE, "env"),
    (BOOTSTRAP_NAME, True, False, ENV_VALUE, "env"),
    (BOOTSTRAP_NAME, False, True, VAULT_VALUE, "vault"),
    (BOOTSTRAP_NAME, False, False, None, "missing"),
]


@pytest.mark.parametrize(("name", "in_env", "in_vault", "value", "source"), PRECEDENCE)
def test_precedence_table(monkeypatch, name, in_env, in_vault, value, source):
    if in_env:
        monkeypatch.setenv(name, ENV_VALUE)
    _vault_holds(monkeypatch, {name: VAULT_VALUE} if in_vault else {})
    resolved = resolve_secret(name)
    assert resolved.value == value
    assert resolved.source == source


def test_a_stale_environment_value_no_longer_shadows_a_fresh_vault_row(monkeypatch):
    """The incident, as one assertion.

    The environment holds the expired token the box booted with; the assistant
    has since written the replacement into the vault. The replacement is what
    every reader must now get, with no restart and no root.
    """
    monkeypatch.setenv(APPLICATION_NAME, ENV_VALUE)
    _vault_holds(monkeypatch, {APPLICATION_NAME: VAULT_VALUE})
    assert resolve_secret(APPLICATION_NAME) == (VAULT_VALUE, "vault")


def test_an_unreadable_vault_falls_back_to_the_environment(monkeypatch):
    """Availability beats precedence when the vault cannot answer at all.

    Failing closed here would mean a vault outage takes every channel, every
    provider and every integration down with it -- for credentials the
    environment is still holding perfectly good copies of. The environment is
    a root-owned file; trusting it when the preferred store is unreachable
    loses nothing an attacker did not already have.
    """
    monkeypatch.setenv(APPLICATION_NAME, ENV_VALUE)
    _vault_down(monkeypatch)
    assert resolve_secret(APPLICATION_NAME) == (ENV_VALUE, "env")


def test_an_unreadable_vault_with_nothing_in_the_environment_is_unavailable(monkeypatch):
    """Still ``unavailable``, never ``missing``: nobody knows, and the callers
    that generate-on-absence must not act on "nobody knows"."""
    _vault_down(monkeypatch)
    assert resolve_secret(APPLICATION_NAME) == (None, "unavailable")


def test_an_explicit_vault_key_is_still_read_directly(monkeypatch):
    """Naming the row does not change which store wins, only which row is read."""
    monkeypatch.setenv(APPLICATION_NAME, ENV_VALUE)
    _vault_holds(monkeypatch, {"providers/github/api_key": VAULT_VALUE})
    resolved = resolve_secret(APPLICATION_NAME, vault_key="providers/github/api_key")
    assert resolved == (VAULT_VALUE, "vault")

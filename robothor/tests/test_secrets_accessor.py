"""``robothor.secrets`` is the one place the platform asks for a credential.

Before it existed, a secret was read four ways: the process environment
(populated from ``/run/robothor/secrets.env`` by systemd, or by the container
env), the AES vault, SOPS, and a ``.env`` file — with ``auth/tokens.py``,
``auth/mfa_secrets.py``, ``engine/key_pool.py`` and assorted bare
``os.environ`` calls each implementing their own subset. "Where does this
instance keep that value?" had no single answer, so neither did "why does the
bridge think it is unset?".

The chain is environment, then vault, then nothing. Both halves are asserted
here, and so are the two things that matter more than either:

* a vault that cannot be read degrades to "unset" — it never raises into a
  caller and never turns an optional credential into a startup crash,
* no value ever reaches a log record.
"""

from __future__ import annotations

import logging

import pytest

from robothor import secrets as secrets_module
from robothor.secrets import get_secret, secret_source

#: Distinctive enough that a substring search for it cannot match by accident.
SENTINEL = "sentinel-value-must-never-be-logged-91b2de"

NAME = "GENUS_TEST_ACCESSOR_KEY"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    secrets_module.reset_vault_availability()
    yield
    secrets_module.reset_vault_availability()


def _vault_raises(monkeypatch, exc: Exception) -> None:
    from robothor import vault

    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(vault, "get", boom)
    monkeypatch.setattr(vault, "export_env", boom)


def _vault_holds(monkeypatch, mapping: dict[str, str]) -> list[str]:
    """Make the vault answer from *mapping*, recording every key asked for."""
    from robothor import vault

    asked: list[str] = []

    def fake_get(key, **kwargs):
        asked.append(key)
        return mapping.get(key)

    def fake_export(**kwargs):
        asked.append("<export>")
        return dict(mapping)

    monkeypatch.setattr(vault, "get", fake_get)
    monkeypatch.setattr(vault, "export_env", fake_export)
    return asked


# ── the environment comes first ──────────────────────────────────────────────


def test_the_process_environment_answers_first(monkeypatch):
    monkeypatch.setenv(NAME, SENTINEL)
    _vault_raises(monkeypatch, AssertionError("the vault must not be touched"))
    assert get_secret(NAME) == SENTINEL
    assert secret_source(NAME) == "env"


def test_an_empty_variable_is_unset_not_empty(monkeypatch):
    """The settings sources treat an empty string as unset; so does this.

    An operator who commented out a value in ``robothor.env`` and left
    ``KEY=`` behind has not configured a credential, and a caller handed ``""``
    would sign tokens with the empty key rather than generate one.
    """
    monkeypatch.setenv(NAME, "   ")
    _vault_holds(monkeypatch, {NAME: SENTINEL})
    assert get_secret(NAME) == SENTINEL
    assert secret_source(NAME) == "vault"


# ── then the vault ───────────────────────────────────────────────────────────


def test_the_vault_answers_under_the_exported_environment_name(monkeypatch):
    """``vault/naming.env_name`` is the one env<->vault mapping.

    It upper-cases and swaps ``/`` for ``_``, which is not invertible, so the
    accessor looks the ENVIRONMENT name up in the vault's own export instead of
    inventing a second naming scheme that would drift from it.
    """
    asked = _vault_holds(monkeypatch, {NAME: SENTINEL})
    assert get_secret(NAME) == SENTINEL
    assert secret_source(NAME) == "vault"
    assert "<export>" in asked


def test_an_explicit_vault_key_is_read_directly(monkeypatch):
    """A caller whose vault row predates the env convention names the row.

    ``auth/tokens.py`` holds its signing key at ``auth/jwt_signing_key``, whose
    exported name is ``AUTH_JWT_SIGNING_KEY`` — not the
    ``GENUS_AUTH_SIGNING_KEY`` the environment uses. Without this the accessor
    would look up a name no existing instance has and generate a new signing
    key on a box that already had one, invalidating every session and every
    stored MFA secret derived from it.
    """
    asked = _vault_holds(monkeypatch, {"auth/jwt_signing_key": SENTINEL})
    assert get_secret(NAME, vault_key="auth/jwt_signing_key") == SENTINEL
    assert asked == ["auth/jwt_signing_key"], "the row was not read directly"


def test_nothing_anywhere_is_missing(monkeypatch):
    _vault_holds(monkeypatch, {})
    assert get_secret(NAME) is None
    assert secret_source(NAME) == "missing"


# ── a vault that cannot be read ──────────────────────────────────────────────


def test_an_unreadable_vault_degrades_to_unset(monkeypatch, caplog):
    """Most instances have no vault at all: no master key, no database, or both.

    Raising here would turn every optional credential into a startup crash,
    which is precisely what ``key_pool`` learned not to do.
    """
    _vault_raises(monkeypatch, FileNotFoundError("no master key"))
    with caplog.at_level(logging.INFO, logger="robothor.secrets"):
        assert get_secret(NAME) is None
        assert secret_source(NAME) == "missing"
    degrades = [r for r in caplog.records if "vault" in r.getMessage()]
    assert len(degrades) == 1, (
        "an unreadable vault must say so exactly once, not once per lookup: "
        f"{[r.getMessage() for r in degrades]}"
    )


def test_the_degrade_line_names_the_secret_but_not_its_value(monkeypatch, caplog):
    _vault_raises(monkeypatch, RuntimeError(f"connection string with {SENTINEL} in it"))
    with caplog.at_level(logging.DEBUG, logger="robothor.secrets"):
        get_secret(NAME)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert NAME in text, "a degrade the operator cannot attribute to a name is not diagnosable"
    assert SENTINEL not in text, "the failure text carried a credential into the log"


def test_a_failed_vault_is_not_retried_on_every_lookup(monkeypatch):
    """A per-call retry is a synchronous database connect on the hot path."""
    from robothor import vault

    calls: list[str] = []

    def boom(*args, **kwargs):
        calls.append("try")
        raise RuntimeError("down")

    monkeypatch.setattr(vault, "export_env", boom)
    monkeypatch.setattr(vault, "get", boom)
    for _ in range(5):
        assert get_secret(NAME) is None
    assert len(calls) == 1, f"the vault was probed {len(calls)} times after failing once"

    secrets_module.reset_vault_availability()
    assert get_secret(NAME) is None
    assert len(calls) == 2, "reset_vault_availability did not re-arm the probe"


def test_a_recovered_vault_is_read_again(monkeypatch):
    """The cooldown is a cooldown, not a permanent verdict."""
    _vault_raises(monkeypatch, RuntimeError("down"))
    assert get_secret(NAME) is None
    monkeypatch.setattr(secrets_module, "_clock", lambda: 10_000_000.0)
    _vault_holds(monkeypatch, {NAME: SENTINEL})
    assert get_secret(NAME) == SENTINEL


# ── logging, in the ordinary case ────────────────────────────────────────────


def test_a_successful_lookup_logs_the_name_and_the_source_only(monkeypatch, caplog):
    monkeypatch.setenv(NAME, SENTINEL)
    with caplog.at_level(logging.DEBUG, logger="robothor.secrets"):
        assert get_secret(NAME) == SENTINEL
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SENTINEL not in text, "a debug line printed the credential"


def test_the_vault_value_never_reaches_a_log_record(monkeypatch, caplog):
    _vault_holds(monkeypatch, {NAME: SENTINEL})
    with caplog.at_level(logging.DEBUG, logger="robothor.secrets"):
        assert get_secret(NAME) == SENTINEL
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SENTINEL not in text


# ── the resolved pair ────────────────────────────────────────────────────────


def test_resolve_returns_the_value_and_its_source_in_one_lookup(monkeypatch):
    """``get_secret`` then ``secret_source`` is two vault round trips for one
    question, so the callers that need both ask once."""
    asked = _vault_holds(monkeypatch, {NAME: SENTINEL})
    resolved = secrets_module.resolve_secret(NAME)
    assert resolved.value == SENTINEL
    assert resolved.source == "vault"
    assert len(asked) == 1, f"one resolve made {len(asked)} vault reads"


def test_the_source_of_a_missing_secret_is_missing_not_none(monkeypatch):
    _vault_holds(monkeypatch, {})
    assert secrets_module.resolve_secret(NAME) == (None, "missing")

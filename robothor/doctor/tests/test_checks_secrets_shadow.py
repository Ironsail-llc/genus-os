"""The check that would have caught the 2026-09-15 incident in one command.

An expired ``GH_TOKEN`` sat in the engine's process environment, loaded at boot
from a root-owned SOPS file. The assistant wrote a fresh one into the vault.
The accessor read the environment first, so every reader kept getting the dead
one — and nothing, anywhere, said the two stores disagreed. The operator's only
symptom was that the assistant "could not" keep a token.

``secrets.shadowed`` asks both stores directly and reports every application
credential they disagree about: names only, never values, with a fingerprint
per store so an operator can tell WHICH one is being served without reading
either. It fails when the shadow is one the old rule would have got wrong —
that is, when the environment value is the dead one and the vault holds the
replacement — and merely recommends otherwise, because two stores holding
different values is untidy long before it is broken.
"""

from __future__ import annotations

import pytest

from robothor.doctor.checks.secrets import CHECKS

ENV_VALUE = "ghp_FAKE0000_the_expired_one"
VAULT_VALUE = "ghp_FAKE1111_the_one_the_assistant_wrote"


def _check():
    found = [check for check in CHECKS if check.id == "secrets.shadowed"]
    assert found, "the shadow check is not registered"
    return found[0]


@pytest.fixture
def ctx(monkeypatch):
    """A doctor context that runs blocking work inline."""

    from robothor.settings import get_settings

    class _Ctx:
        settings = get_settings()

        async def run_blocking(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    from robothor import secrets as secrets_module

    secrets_module.reset_vault_availability()
    yield _Ctx()
    secrets_module.reset_vault_availability()


def _stores(monkeypatch, env: dict[str, str], vault_rows: dict[str, str]) -> None:
    """Stand both stores up. ``vault_rows`` is keyed by ENVIRONMENT name.

    Translated through ``vault_keys_for_env_name``, which is what the accessor
    and the status module both search with. The double used to be keyed by
    environment name directly and answer through ``export_env`` — a faithful
    model of an implementation that decrypted every row in the instance to
    answer about one, and which R9 removed.
    """
    from robothor import vault
    from robothor.vault.naming import vault_keys_for_env_name

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    rows = {vault_keys_for_env_name(name)[0]: value for name, value in vault_rows.items()}

    # The table is scoped to the names this test set, not to the environment of
    # whoever runs the suite. Without this the runner's own credentials join the
    # table — and an ALIAS of one of them (this machine has GH_TOKEN, which
    # resolves to the same vault row as GITHUB_TOKEN) reports a perfectly real
    # shadow that has nothing to do with the assertion. It also keeps the
    # runner's environment out of any failure output, which is the rule this
    # whole task is about.
    from robothor.secrets import status as status_module
    from robothor.vault.naming import env_names_for_vault_key

    monkeypatch.setattr(status_module, "environment_credential_names", lambda: set(env))

    # An ALIAS of a name under test must not be answered by the runner's own
    # environment. This machine has GH_TOKEN set, which resolves to the same
    # vault row as GITHUB_TOKEN, so the table reported a perfectly real shadow
    # between the runner's token and the fixture's — a true statement about
    # nothing the test is asserting.
    for key in rows:
        for alias in env_names_for_vault_key(key):
            if alias not in env:
                monkeypatch.delenv(alias, raising=False)
    monkeypatch.setattr(
        vault, "export_env", lambda **kw: {k.upper().replace("/", "_"): v for k, v in rows.items()}
    )
    monkeypatch.setattr(vault, "get", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(vault, "list", lambda **kw: sorted(rows))


@pytest.mark.asyncio
async def test_agreeing_stores_are_not_a_shadow(ctx, monkeypatch):
    """The same value in both places is how a migration leaves a box. It is
    not news, and a check that called it one would be muted within a week."""
    _stores(monkeypatch, {"GITHUB_TOKEN": VAULT_VALUE}, {"GITHUB_TOKEN": VAULT_VALUE})
    result = await _check().run(ctx)
    assert result.status == "pass"


@pytest.mark.asyncio
async def test_a_disagreement_is_reported_by_name_and_never_by_value(ctx, monkeypatch):
    _stores(monkeypatch, {"GITHUB_TOKEN": ENV_VALUE}, {"GITHUB_TOKEN": VAULT_VALUE})
    result = await _check().run(ctx)
    assert "GITHUB_TOKEN" in result.detail
    assert ENV_VALUE not in result.detail
    assert VAULT_VALUE not in result.detail


@pytest.mark.asyncio
async def test_the_detail_says_which_store_is_being_served(ctx, monkeypatch):
    """An operator reading "these disagree" and not "and this one wins" has to
    go and find out, which is the state this check exists to end."""
    _stores(monkeypatch, {"GITHUB_TOKEN": ENV_VALUE}, {"GITHUB_TOKEN": VAULT_VALUE})
    result = await _check().run(ctx)
    assert "vault" in result.detail.lower()


@pytest.mark.asyncio
async def test_a_shadow_the_old_rule_would_have_got_wrong_fails(ctx, monkeypatch):
    """The incident's exact shape: an application credential in both stores,
    disagreeing, where the environment would have won under the old chain."""
    _stores(monkeypatch, {"GITHUB_TOKEN": ENV_VALUE}, {"GITHUB_TOKEN": VAULT_VALUE})
    result = await _check().run(ctx)
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_a_bootstrap_credential_in_both_stores_is_not_a_failure(ctx, monkeypatch):
    """Bootstrap credentials are environment-first BY DESIGN, so a vault copy
    of one is a leftover, not a shadow. Failing on it would fire the check on
    every box that ever ran the migration and teach operators to ignore it."""
    _stores(
        monkeypatch,
        {"ROBOTHOR_DB_PASSWORD": "fake-live-0000"},
        {"ROBOTHOR_DB_PASSWORD": "fake-stale-1111"},
    )
    result = await _check().run(ctx)
    assert result.status != "fail"


@pytest.mark.asyncio
async def test_an_unreadable_vault_never_reports_green(ctx, monkeypatch):
    """Review finding I2, and the inert-control shape exactly.

    The check swallowed an unreadable vault and returned "no credential is
    configured differently in the environment and the vault" — which is true
    only in the sense that it compared nothing. A dead environment value could
    be served while the control that exists to say so reported green.

    ``skip`` rather than ``fail``: a box with no vault at all is a valid
    instance and would otherwise fail forever. ``skip`` is never a silent pass
    — the doctor reports it, and it says why.
    """
    from robothor import vault

    def boom(*args, **kwargs):
        raise RuntimeError("no master key")

    monkeypatch.setenv("GITHUB_TOKEN", ENV_VALUE)
    monkeypatch.setattr(vault, "export_env", boom)
    monkeypatch.setattr(vault, "get", boom)
    monkeypatch.setattr(vault, "list", boom)
    result = await _check().run(ctx)
    assert result.status == "skip", (
        "an unreadable vault reported as a pass: the check compared nothing and "
        "said nothing was wrong"
    )
    assert "vault" in result.detail.lower()

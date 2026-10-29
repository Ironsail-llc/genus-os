"""The secret cache must not carry a value across a tenant binding.

``robothor.secrets`` caches what the vault answered, keyed by ``(tenant, key)``.
Once the vault's connection is RLS-bound to the enclosing ``tenant_scope``, the
same ``(tenant, key)`` question has different answers depending on the binding:
a root-tenant read that was cached and then served inside a child scope hands
the child a credential the database itself would have refused. The binding is
therefore part of the cache key.

And the signing key is the instance's, not a tenant's: resolving or generating
it inside a child scope must still read and write the root tenant's row, or
the first token minted inside a child request would rotate every session.
"""

from __future__ import annotations

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import effective_tenant, tenant_scope


@pytest.fixture
def binding_aware_vault(monkeypatch: pytest.MonkeyPatch):
    """A vault that answers only for the root tenant's binding, like RLS would."""
    from robothor import secrets as secrets_module
    from robothor import vault

    calls: list[str] = []

    def fake_get(key, **kwargs):
        calls.append(effective_tenant())
        if effective_tenant() in ("", DEFAULT_TENANT):
            return "root-only-value"
        return None

    monkeypatch.setattr(vault, "get", fake_get)
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", DEFAULT_TENANT)
    monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)
    secrets_module.reset_vault_availability()
    yield calls
    secrets_module.reset_vault_availability()


def test_a_root_hit_is_not_served_inside_a_child_scope(binding_aware_vault) -> None:
    from robothor.secrets import get_secret

    assert get_secret("SOME_APP_TOKEN", vault_key="svc/token") == "root-only-value"
    with tenant_scope("acme-child-a"):
        assert get_secret("SOME_APP_TOKEN", vault_key="svc/token") is None
    assert len(binding_aware_vault) == 2, "the child-scope read was answered from the cache"


def test_a_child_miss_does_not_poison_the_root_read(binding_aware_vault) -> None:
    from robothor.secrets import get_secret

    with tenant_scope("acme-child-a"):
        assert get_secret("SOME_APP_TOKEN", vault_key="svc/token") is None
    assert get_secret("SOME_APP_TOKEN", vault_key="svc/token") == "root-only-value"


def test_signing_key_resolves_against_the_root_tenant_inside_a_child_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robothor import secrets as secrets_module
    from robothor.auth import tokens

    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    seen: list[tuple[str, str]] = []

    def fake_resolve(name, *, vault_key=None, tenant_id=DEFAULT_TENANT, live=False):
        seen.append((tenant_id, effective_tenant()))
        return secrets_module.ResolvedSecret("k" * 48, "vault")

    monkeypatch.setattr(secrets_module, "resolve_secret", fake_resolve)
    tokens.reset_signing_key_cache()
    try:
        with tenant_scope("acme-child-a"):
            tokens.signing_key()
    finally:
        tokens.reset_signing_key_cache()
    assert seen == [(DEFAULT_TENANT, DEFAULT_TENANT)]


def test_a_generated_signing_key_is_stored_in_the_root_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robothor import secrets as secrets_module
    from robothor.auth import tokens
    from robothor.vault import dal

    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    monkeypatch.setattr(
        secrets_module,
        "resolve_secret",
        lambda *a, **k: secrets_module.ResolvedSecret(None, "missing"),
    )
    stored: list[tuple[str, str]] = []

    def fake_set_secret(
        key, value, master_key, *, category="credential", metadata=None, tenant_id=None
    ):
        from robothor.db.connection import current_tenant_scope

        stored.append((tenant_id or current_tenant_scope() or DEFAULT_TENANT, effective_tenant()))

    import robothor.vault as vault

    monkeypatch.setattr(vault, "get_master_key", lambda: b"0" * 32)
    monkeypatch.setattr(vault, "set_secret", fake_set_secret)
    monkeypatch.setattr(dal, "set_secret", fake_set_secret)
    tokens.reset_signing_key_cache()
    try:
        with tenant_scope("acme-child-a"):
            tokens.signing_key()
    finally:
        tokens.reset_signing_key_cache()
    assert stored == [(DEFAULT_TENANT, DEFAULT_TENANT)]

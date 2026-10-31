"""
Genus OS Vault — native credential store backed by PostgreSQL + AES-256-GCM.

Public API:
    vault.get(key)           → decrypted value or None
    vault.set(key, value)    → store encrypted
    vault.delete(key)        → remove
    vault.list_keys()        → list keys (not values)
    vault.export_env()       → all secrets as {KEY: VALUE} dict

``tenant_id=None`` (every function's default) means the enclosing
``robothor.db.connection.tenant_scope``, and the default tenant outside one;
``robothor.vault.dal`` resolves it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.vault.crypto import get_master_key, init_master_key
from robothor.vault.dal import (
    delete_secret,
    export_secrets,
    get_secret,
    list_keys,
    set_secret,
)
from robothor.vault.naming import normalise_key

if TYPE_CHECKING:
    import builtins


def get(key: str, *, tenant_id: str | None = None) -> str | None:
    """Retrieve and decrypt a secret by key. Returns None if not found."""
    master_key = get_master_key()
    return get_secret(normalise_key(key), master_key, tenant_id=tenant_id)


def set(  # noqa: A001
    key: str, value: str, *, category: str = "credential", tenant_id: str | None = None
) -> None:
    """Encrypt and store a secret.

    The key is normalised before it is stored. Without this,
    ``'ROBOTHOR_DB_PASSWORD '`` — one trailing space — was a different key from
    the same name without it: it slipped past the bootstrap refusal and landed
    under a padded key that no reader would ever find. Harmless in isolation
    and exactly the shape of a precedence bypass, so it is normalised at the
    one door every write goes through.
    """
    master_key = get_master_key()
    set_secret(normalise_key(key), value, master_key, category=category, tenant_id=tenant_id)


def delete(key: str, *, tenant_id: str | None = None) -> bool:
    """Delete a secret by key. Returns True if deleted."""
    return delete_secret(normalise_key(key), tenant_id=tenant_id)


def list(  # noqa: A001
    *, category: str | None = None, tenant_id: str | None = None
) -> builtins.list[str]:
    """List all secret keys, optionally filtered by category."""
    return list_keys(category=category, tenant_id=tenant_id)


def export_env(*, tenant_id: str | None = None) -> dict[str, str]:
    """Export all secrets as {KEY: VALUE} dict. Keys are uppercased with / replaced by _."""
    master_key = get_master_key()
    return export_secrets(master_key, tenant_id=tenant_id)


__all__ = ["get", "set", "delete", "list", "export_env", "init_master_key"]

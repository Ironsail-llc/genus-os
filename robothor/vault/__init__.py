"""
Robothor Vault — native credential store backed by PostgreSQL + AES-256-GCM.

Public API:
    vault.get(key)           → decrypted value or None
    vault.set(key, value)    → store encrypted
    vault.delete(key)        → remove
    vault.list_keys()        → list keys (not values)
    vault.export_env()       → all secrets as {KEY: VALUE} dict
"""

from __future__ import annotations

from robothor.vault.crypto import get_master_key, init_master_key
from robothor.vault.dal import (
    delete_secret,
    export_secrets,
    get_secret,
    list_keys,
    set_secret,
)

DEFAULT_TENANT = "robothor-primary"


def get(key: str, *, tenant_id: str = DEFAULT_TENANT) -> str | None:
    """Retrieve and decrypt a secret by key. Returns None if not found."""
    master_key = get_master_key()
    return get_secret(key, master_key, tenant_id=tenant_id)


def set(key: str, value: str, *, category: str = "credential", tenant_id: str = DEFAULT_TENANT) -> None:
    """Encrypt and store a secret."""
    master_key = get_master_key()
    set_secret(key, value, master_key, category=category, tenant_id=tenant_id)


def delete(key: str, *, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Delete a secret by key. Returns True if deleted."""
    return delete_secret(key, tenant_id=tenant_id)


def list(*, category: str | None = None, tenant_id: str = DEFAULT_TENANT) -> list[str]:
    """List all secret keys, optionally filtered by category."""
    return list_keys(category=category, tenant_id=tenant_id)


def export_env(*, tenant_id: str = DEFAULT_TENANT) -> dict[str, str]:
    """Export all secrets as {KEY: VALUE} dict. Keys are uppercased with / replaced by _."""
    master_key = get_master_key()
    return export_secrets(master_key, tenant_id=tenant_id)


__all__ = ["get", "set", "delete", "list", "export_env", "init_master_key"]

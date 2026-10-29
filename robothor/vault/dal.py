"""
Vault DAL — CRUD operations on vault_secrets table.

Uses psycopg2 directly (same pattern as robothor.crm.dal).

Tenant: every function takes ``tenant_id: str | None = None``. ``None`` means
the enclosing :func:`robothor.db.connection.tenant_scope` — the bridge binds
each request to its token's tenant — and the default tenant outside one. The
connection is bound for RLS the same way ``get_connection`` binds a pooled
one, so with ``ROBOTHOR_RLS_ENABLED`` set the database itself refuses another
tenant's rows even if a WHERE clause names them.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.vault.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)


def _get_conn() -> Any:
    """A database connection using the standard Genus OS config, RLS-bound.

    Not pooled (the vault predates the pool and is read rarely), but bound
    exactly as a pooled checkout is: ``_apply_tenant_scope`` sets
    ``app.tenant_id`` from the enclosing scope, so the vault was no longer the
    one tenant table the policy could not protect.
    """
    import psycopg2

    from robothor.config import get_config
    from robothor.db.connection import _apply_tenant_scope

    cfg = get_config().db
    conn = psycopg2.connect(**cfg.dict, connect_timeout=5)
    try:
        _apply_tenant_scope(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _tenant(tenant_id: str | None) -> str:
    """An explicit tenant, else the enclosing scope's, else the default."""
    if tenant_id is not None:
        return tenant_id
    from robothor.db.connection import current_tenant_scope

    return current_tenant_scope() or DEFAULT_TENANT


def set_secret(
    key: str,
    value: str,
    master_key: bytes,
    *,
    category: str = "credential",
    metadata: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> None:
    """Encrypt and upsert a secret."""
    tenant_id = _tenant(tenant_id)
    encrypted = encrypt(value, master_key)
    meta_json = json.dumps(metadata or {})

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vault_secrets (tenant_id, key, encrypted_value, category, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (tenant_id, key)
                DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value,
                              category = EXCLUDED.category,
                              metadata = EXCLUDED.metadata,
                              updated_at = EXCLUDED.updated_at
                """,
                (tenant_id, key, encrypted, category, meta_json, datetime.now(UTC)),
            )
        conn.commit()
    finally:
        conn.close()


def get_secret(
    key: str,
    master_key: bytes,
    *,
    tenant_id: str | None = None,
) -> str | None:
    """Retrieve and decrypt a secret. Returns None if not found."""
    tenant_id = _tenant(tenant_id)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT encrypted_value FROM vault_secrets WHERE tenant_id = %s AND key = %s",
                (tenant_id, key),
            )
            row = cur.fetchone()
            if not row:
                return None
            return decrypt(bytes(row[0]), master_key)
    finally:
        conn.close()


def get_secrets_updated_at(
    keys: list[str],
    *,
    tenant_id: str | None = None,
) -> dict[str, datetime]:
    """When each of these secrets was last written, without decrypting any.

    Bulk rather than per key, and separate from ``get_secret``, for two
    different reasons. Separate, because a status page wants to say "set three
    days ago" and has no business holding the plaintext to do it — no master
    key is needed to answer. Bulk, because the caller is a provider listing
    with up to five providers times sixteen slots, and one connection per slot
    is how a status page becomes a database incident.

    Keys with no row are simply absent from the result.
    """
    tenant_id = _tenant(tenant_id)
    if not keys:
        return {}
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, updated_at FROM vault_secrets WHERE tenant_id = %s AND key = ANY(%s)",
                (tenant_id, list(keys)),
            )
            return {row[0]: row[1] for row in cur.fetchall() if row[1] is not None}
    finally:
        conn.close()


def delete_secret(key: str, *, tenant_id: str | None = None) -> bool:
    """Delete a secret. Returns True if a row was deleted."""
    tenant_id = _tenant(tenant_id)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM vault_secrets WHERE tenant_id = %s AND key = %s",
                (tenant_id, key),
            )
            deleted = bool(cur.rowcount > 0)
        conn.commit()
        return deleted
    finally:
        conn.close()


def list_keys(
    *,
    category: str | None = None,
    tenant_id: str | None = None,
) -> list[str]:
    """List all secret keys, optionally filtered by category."""
    tenant_id = _tenant(tenant_id)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if category:
                cur.execute(
                    "SELECT key FROM vault_secrets WHERE tenant_id = %s AND category = %s ORDER BY key",
                    (tenant_id, category),
                )
            else:
                cur.execute(
                    "SELECT key FROM vault_secrets WHERE tenant_id = %s ORDER BY key",
                    (tenant_id,),
                )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def export_secrets(
    master_key: bytes,
    *,
    tenant_id: str | None = None,
) -> dict[str, str]:
    """Export all secrets as {KEY: VALUE}. Keys are uppercased with / → _."""
    tenant_id = _tenant(tenant_id)
    conn = _get_conn()
    try:
        result: dict[str, str] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, encrypted_value FROM vault_secrets WHERE tenant_id = %s ORDER BY key",
                (tenant_id,),
            )
            for row in cur.fetchall():
                env_key = row[0].upper().replace("/", "_")
                result[env_key] = decrypt(bytes(row[1]), master_key)
        return result
    finally:
        conn.close()


def count_secrets(*, tenant_id: str | None = None) -> int:
    """Count secrets in the vault."""
    tenant_id = _tenant(tenant_id)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM vault_secrets WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            return row[0] if row else 0
    finally:
        conn.close()

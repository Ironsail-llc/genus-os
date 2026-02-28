"""
Vault DAL — CRUD operations on vault_secrets table.

Uses psycopg2 directly (same pattern as robothor.crm.dal).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from robothor.vault.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "robothor-primary"


def _get_conn():
    """Get a database connection using the standard Robothor config."""
    import psycopg2

    from robothor.config import get_config

    cfg = get_config().db
    return psycopg2.connect(**cfg.dict, connect_timeout=5)


def set_secret(
    key: str,
    value: str,
    master_key: bytes,
    *,
    category: str = "credential",
    metadata: dict | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Encrypt and upsert a secret."""
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
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Retrieve and decrypt a secret. Returns None if not found."""
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


def delete_secret(key: str, *, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Delete a secret. Returns True if a row was deleted."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM vault_secrets WHERE tenant_id = %s AND key = %s",
                (tenant_id, key),
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def list_keys(
    *,
    category: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> list[str]:
    """List all secret keys, optionally filtered by category."""
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
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, str]:
    """Export all secrets as {KEY: VALUE}. Keys are uppercased with / → _."""
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


def count_secrets(*, tenant_id: str = DEFAULT_TENANT) -> int:
    """Count secrets in the vault."""
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

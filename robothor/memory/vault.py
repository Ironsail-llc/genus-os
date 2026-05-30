"""Knowledge Vault — verbatim reference store for the memory system.

A MIRIX-style vault for data the agent must recall *exactly* (contact
numbers, account/case ids, addresses, bookmarks, credentials). Unlike
``memory_facts`` (LLM-extracted, paraphrased), vault entries preserve the
original string byte-for-byte.

Safety model:
    * Only the **caption** is embedded and searchable — the value is never
      vectorized, and ``search_vault`` never returns a value.
    * ``high`` sensitivity values are encrypted at rest with AES-256-GCM via
      the existing ``robothor.vault.crypto`` master key; ``low``/``medium``
      values are stored verbatim in ``value_exact``.
    * Every value read goes through ``get_vault_value``, which writes a
      ``vault_access_log`` row.

This is NOT the secrets vault (``robothor.vault`` / ``vault_secrets``). It is
a searchable, tenant-scoped memory store. Gated by ``ROBOTHOR_RIP_12_ENABLED``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client
from robothor.memory.vector_tuning import apply_hnsw_session

logger = logging.getLogger(__name__)

VALID_ENTRY_TYPES = frozenset(
    {"contact_info", "account_id", "address", "bookmark", "credential", "api_key"}
)
VALID_SENSITIVITY = frozenset({"low", "medium", "high"})


def _encrypt_value(value: str) -> bytes:
    """Encrypt a high-sensitivity value with the shared vault master key."""
    from robothor.vault.crypto import encrypt, get_master_key

    return encrypt(value, get_master_key())


def _decrypt_value(blob: bytes) -> str:
    """Decrypt a high-sensitivity value with the shared vault master key."""
    from robothor.vault.crypto import decrypt, get_master_key

    return decrypt(bytes(blob), get_master_key())


async def store_vault_entry(
    caption: str,
    value: str,
    *,
    entry_type: str,
    sensitivity: str = "medium",
    source: str = "",
    entity_id: int | None = None,
    person_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    tenant_id: str = "",
) -> int:
    """Store (or upsert) a verbatim vault entry. Returns the row id.

    The caption is embedded for search; the value is stored plaintext for
    low/medium sensitivity, or encrypted at rest for ``high``.
    """
    if entry_type not in VALID_ENTRY_TYPES:
        raise ValueError(f"invalid entry_type {entry_type!r} (valid: {sorted(VALID_ENTRY_TYPES)})")
    if sensitivity not in VALID_SENSITIVITY:
        raise ValueError(
            f"invalid sensitivity {sensitivity!r} (valid: {sorted(VALID_SENSITIVITY)})"
        )

    resolved_tenant = tenant_id or DEFAULT_TENANT
    caption_embedding = await llm_client.get_embedding_async(caption)

    if sensitivity == "high":
        value_exact: str | None = None
        value_enc: bytes | None = _encrypt_value(value)
    else:
        value_exact = value
        value_enc = None

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_vault
                (tenant_id, entry_type, caption, value_exact, value_enc, sensitivity,
                 source, entity_id, person_id, caption_embedding, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, entry_type, md5(caption)) WHERE is_active
            DO UPDATE SET
                value_exact = EXCLUDED.value_exact,
                value_enc = EXCLUDED.value_enc,
                sensitivity = EXCLUDED.sensitivity,
                source = EXCLUDED.source,
                entity_id = EXCLUDED.entity_id,
                person_id = EXCLUDED.person_id,
                caption_embedding = EXCLUDED.caption_embedding,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            RETURNING id
            """,
            (
                resolved_tenant,
                entry_type,
                caption,
                value_exact,
                value_enc,
                sensitivity,
                source,
                entity_id,
                person_id,
                caption_embedding,
                json.dumps(metadata or {}),
            ),
        )
        entry_id: int = cur.fetchone()[0]

    return entry_id


async def search_vault(
    query: str,
    *,
    entry_type: str | None = None,
    limit: int = 5,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Semantic search over captions. Never returns the stored value.

    Use ``get_vault_value(id)`` to retrieve the exact value (audited).
    """
    resolved_tenant = tenant_id or DEFAULT_TENANT
    embedding = await llm_client.get_embedding_async(query)

    type_clause = "AND entry_type = %s" if entry_type else ""
    # Placeholder order: SELECT embedding, WHERE tenant, [entry_type], ORDER BY embedding, LIMIT.
    params: list[Any] = [embedding, resolved_tenant]
    if entry_type:
        params.append(entry_type)
    params.extend([embedding, limit])

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        apply_hnsw_session(cur)
        cur.execute(
            f"""
            SELECT id, caption, entry_type, sensitivity, source, created_at,
                   1 - (caption_embedding <=> %s::vector) AS similarity
            FROM memory_vault
            WHERE caption_embedding IS NOT NULL
              AND tenant_id = %s
              AND is_active = TRUE
              {type_clause}
            ORDER BY caption_embedding <=> %s::vector
            LIMIT %s
            """,
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        r["similarity"] = round(r.get("similarity") or 0.0, 4)
    return rows


def get_vault_value(
    entry_id: int,
    *,
    tenant_id: str = "",
    run_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Retrieve the exact value for an entry, decrypting high-sensitivity rows.

    Writes a ``vault_access_log`` row for every read. Returns ``{"error": ...}``
    when the entry is missing or belongs to another tenant.
    """
    resolved_tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, caption, entry_type, sensitivity, value_exact, value_enc
            FROM memory_vault
            WHERE id = %s AND tenant_id = %s AND is_active = TRUE
            """,
            (entry_id, resolved_tenant),
        )
        row = cur.fetchone()
        if not row:
            return {"error": "not_found", "id": entry_id}

        if row["value_enc"] is not None:
            value = _decrypt_value(row["value_enc"])
        else:
            value = row["value_exact"]

        # Audit the read (same transaction).
        cur.execute(
            """
            INSERT INTO vault_access_log (tenant_id, vault_id, sensitivity, agent_id, run_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (resolved_tenant, entry_id, row["sensitivity"], agent_id, run_id),
        )

    return {
        "id": row["id"],
        "caption": row["caption"],
        "entry_type": row["entry_type"],
        "sensitivity": row["sensitivity"],
        "value": value,
    }


def list_vault(
    *,
    entry_type: str | None = None,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """List vault entries (captions + metadata only, no values)."""
    resolved_tenant = tenant_id or DEFAULT_TENANT
    type_clause = "AND entry_type = %s" if entry_type else ""
    params: list[Any] = [resolved_tenant]
    if entry_type:
        params.append(entry_type)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"""
            SELECT id, caption, entry_type, sensitivity, source, created_at, updated_at
            FROM memory_vault
            WHERE tenant_id = %s AND is_active = TRUE {type_clause}
            ORDER BY updated_at DESC
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def deactivate_entry(entry_id: int, *, tenant_id: str = "") -> bool:
    """Soft-delete an entry. Returns True if a row was deactivated."""
    resolved_tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_vault SET is_active = FALSE, updated_at = NOW()
            WHERE id = %s AND tenant_id = %s AND is_active = TRUE
            """,
            (entry_id, resolved_tenant),
        )
        return cur.rowcount > 0

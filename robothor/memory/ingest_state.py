"""
Ingestion State Management — dedup, watermarks, content hashing.

Shared by all pipeline tiers for coordinated, duplicate-free ingestion.

Tables: ingestion_watermarks, ingested_items (in robothor_memory DB).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)


def content_hash(data: dict[str, Any], keys: list[str]) -> str:
    """Deterministic SHA-256 hash of selected fields from a dict.

    Args:
        data: Source dict.
        keys: Fields to include in the hash (order matters).

    Returns:
        64-char hex digest.
    """
    parts = []
    for key in sorted(keys):  # sorted for determinism
        val = data.get(key, "")
        if val is None:
            val = ""
        parts.append(f"{key}={val}")
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_already_ingested(source: str, item_id: str, hash_val: str) -> bool:
    """Check if an item was already ingested with the same content hash.

    Returns True if item exists AND hash matches (no change).
    Returns False if item is new OR hash changed (needs re-ingestion).
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT content_hash FROM ingested_items WHERE source_name = %s AND item_id = %s",
            (source, item_id),
        )
        row = cur.fetchone()
        if row is None:
            return False
        matches: bool = row[0] == hash_val
        return matches


def record_ingested(
    source: str,
    item_id: str,
    hash_val: str,
    fact_ids: list[int] | None = None,
) -> None:
    """Record that an item was ingested (upsert)."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ingested_items (source_name, item_id, content_hash, fact_ids, ingested_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (source_name, item_id)
            DO UPDATE SET content_hash = EXCLUDED.content_hash,
                          fact_ids = EXCLUDED.fact_ids,
                          ingested_at = NOW()
            """,
            (source, item_id, hash_val, fact_ids or []),
        )


def get_watermark(source: str) -> dict[str, Any] | None:
    """Read watermark for a source. Returns None if no watermark set."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_ingested_at, items_ingested, last_error, error_count, updated_at "
            "FROM ingestion_watermarks WHERE source_name = %s",
            (source,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "last_ingested_at": row[0],
            "items_ingested": row[1],
            "last_error": row[2],
            "error_count": row[3],
            "updated_at": row[4],
        }


def update_watermark(source: str, items_ingested: int = 0) -> None:
    """Update watermark for a source (upsert). Resets error state on success."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ingestion_watermarks (source_name, last_ingested_at, items_ingested, error_count, updated_at)
            VALUES (%s, NOW(), %s, 0, NOW())
            ON CONFLICT (source_name)
            DO UPDATE SET last_ingested_at = NOW(),
                          items_ingested = ingestion_watermarks.items_ingested + EXCLUDED.items_ingested,
                          error_count = 0,
                          last_error = NULL,
                          updated_at = NOW()
            """,
            (source, items_ingested),
        )


def record_error(source: str, error_msg: str) -> int:
    """Record an error for a source. Returns the new error_count."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ingestion_watermarks (source_name, last_error, error_count, updated_at)
            VALUES (%s, %s, 1, NOW())
            ON CONFLICT (source_name)
            DO UPDATE SET last_error = EXCLUDED.last_error,
                          error_count = ingestion_watermarks.error_count + 1,
                          updated_at = NOW()
            RETURNING error_count
            """,
            (source, error_msg[:2000]),
        )
        error_count: int = cur.fetchone()[0]
        return error_count


def cleanup_old_items(days: int = 90) -> int:
    """Remove ingested_items older than N days. Returns count deleted."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM ingested_items WHERE ingested_at < NOW() - INTERVAL '%s days'",
            (days,),
        )
        deleted: int = cur.rowcount
        return deleted

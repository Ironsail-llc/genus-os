"""
Persistent chat session store — PostgreSQL DAL for conversation history.

Stores chat sessions (metadata + model override) and individual messages
as JSONB (LangChain pattern). In-memory dicts in telegram.py and chat.py
remain the hot path; this module provides durability across engine restarts.

All DB writes are fire-and-forget via async wrappers. Reads happen once
at startup via load_all_sessions().
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)


# ── Sync functions (called from async wrappers via run_in_executor) ──


def upsert_session(
    session_key: str,
    channel: str = "telegram",
    model_override: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> int:
    """Create or update a session row. Returns the session id."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            INSERT INTO chat_sessions (tenant_id, session_key, channel, model_override)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, session_key) DO UPDATE SET
                last_active_at = NOW(),
                model_override = COALESCE(%s, chat_sessions.model_override)
            RETURNING id
            """,
            (tenant_id, session_key, channel, model_override, model_override),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["id"])


def save_exchange(
    session_key: str,
    user_content: str,
    assistant_content: str,
    channel: str = "telegram",
    model_override: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> list[int]:
    """Upsert session + insert user and assistant messages in one transaction.

    Returns the inserted chat_messages ids (order: [user_id, assistant_id])
    so the async caller can schedule embedding without re-querying.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Upsert session
        cur.execute(
            """
            INSERT INTO chat_sessions (tenant_id, session_key, channel, model_override, message_count)
            VALUES (%s, %s, %s, %s, 2)
            ON CONFLICT (tenant_id, session_key) DO UPDATE SET
                last_active_at = NOW(),
                message_count = chat_sessions.message_count + 2,
                model_override = COALESCE(%s, chat_sessions.model_override)
            RETURNING id
            """,
            (tenant_id, session_key, channel, model_override, model_override),
        )
        session_id = cur.fetchone()["id"]

        # Insert both messages
        inserted_ids: list[int] = []
        for role, content in [("user", user_content), ("assistant", assistant_content)]:
            cur.execute(
                """
                INSERT INTO chat_messages (session_id, message)
                VALUES (%s, %s::jsonb)
                RETURNING id
                """,
                (session_id, json.dumps({"role": role, "content": content})),
            )
            inserted_ids.append(int(cur.fetchone()["id"]))

        conn.commit()
        return inserted_ids


def save_message(
    session_key: str,
    role: str,
    content: str,
    channel: str = "telegram",
    tenant_id: str = DEFAULT_TENANT,
) -> int | None:
    """Save a single message (used for system injections). Returns the message id."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            INSERT INTO chat_sessions (tenant_id, session_key, channel, message_count)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (tenant_id, session_key) DO UPDATE SET
                last_active_at = NOW(),
                message_count = chat_sessions.message_count + 1
            RETURNING id
            """,
            (tenant_id, session_key, channel),
        )
        session_id = cur.fetchone()["id"]

        cur.execute(
            """
            INSERT INTO chat_messages (session_id, message)
            VALUES (%s, %s::jsonb)
            RETURNING id
            """,
            (session_id, json.dumps({"role": role, "content": content})),
        )
        message_id = int(cur.fetchone()["id"])

        conn.commit()
        return message_id


def load_session(
    session_key: str,
    limit: int = 20,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Load messages + model_override for one session.

    Returns {"history": [...], "model_override": str|None} or empty dict if not found.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT s.id, s.model_override
            FROM chat_sessions s
            WHERE s.tenant_id = %s AND s.session_key = %s
            """,
            (tenant_id, session_key),
        )
        session_row = cur.fetchone()
        if not session_row:
            return {}

        cur.execute(
            """
            SELECT message
            FROM chat_messages
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (session_row["id"], limit),
        )
        rows = cur.fetchall()

        # Reverse to chronological order (fetched DESC for LIMIT)
        messages = [row["message"] for row in reversed(rows)]

        return {
            "history": messages,
            "model_override": session_row["model_override"],
        }


def load_all_sessions(
    limit_per_session: int = 20,
    ttl_days: int = 7,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, dict[str, Any]]:
    """Load all active sessions with their messages.

    Returns {session_key: {"history": [...], "model_override": str|None}}.
    Used at startup to populate in-memory caches.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Get active sessions within TTL
        cur.execute(
            """
            SELECT id, session_key, model_override, plan_state
            FROM chat_sessions
            WHERE tenant_id = %s
              AND last_active_at >= NOW() - INTERVAL '%s days'
            ORDER BY last_active_at DESC
            """,
            (tenant_id, ttl_days),
        )
        sessions = cur.fetchall()

        if not sessions:
            return {}

        result: dict[str, dict[str, Any]] = {}

        for sess in sessions:
            cur.execute(
                """
                SELECT message
                FROM chat_messages
                WHERE session_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (sess["id"], limit_per_session),
            )
            rows = cur.fetchall()
            messages = [row["message"] for row in reversed(rows)]

            data: dict[str, Any] = {
                "history": messages,
                "model_override": sess["model_override"],
            }
            if sess.get("plan_state"):
                data["plan_state"] = sess["plan_state"]
            result[sess["session_key"]] = data

        return result


def clear_session(
    session_key: str,
    tenant_id: str = DEFAULT_TENANT,
) -> bool:
    """Delete a session and its messages (CASCADE). Returns True if a row was deleted."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM chat_sessions
            WHERE tenant_id = %s AND session_key = %s
            """,
            (tenant_id, session_key),
        )
        conn.commit()
        return bool(cur.rowcount > 0)


def update_model_override(
    session_key: str,
    model_id: str | None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Update just the model_override column on an existing session."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE chat_sessions
            SET model_override = %s, last_active_at = NOW()
            WHERE tenant_id = %s AND session_key = %s
            """,
            (model_id, tenant_id, session_key),
        )
        conn.commit()


def save_plan_state(
    session_key: str,
    plan_dict: dict[str, Any],
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Persist plan state JSONB to the chat_sessions row."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE chat_sessions
            SET plan_state = %s::jsonb, last_active_at = NOW()
            WHERE tenant_id = %s AND session_key = %s
            """,
            (json.dumps(plan_dict), tenant_id, session_key),
        )
        if cur.rowcount == 0:
            # Session row doesn't exist yet — upsert it
            cur.execute(
                """
                INSERT INTO chat_sessions (tenant_id, session_key, channel, plan_state)
                VALUES (%s, %s, 'telegram', %s::jsonb)
                ON CONFLICT (tenant_id, session_key) DO UPDATE SET
                    plan_state = EXCLUDED.plan_state,
                    last_active_at = NOW()
                """,
                (tenant_id, session_key, json.dumps(plan_dict)),
            )
        conn.commit()


def clear_plan_state(
    session_key: str,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Clear plan state (set to NULL) on the chat_sessions row."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE chat_sessions
            SET plan_state = NULL, last_active_at = NOW()
            WHERE tenant_id = %s AND session_key = %s
            """,
            (tenant_id, session_key),
        )
        conn.commit()


def load_plan_state(
    session_key: str,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any] | None:
    """Load plan state from DB. Returns the dict or None if not found/empty."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT plan_state
            FROM chat_sessions
            WHERE tenant_id = %s AND session_key = %s
              AND plan_state IS NOT NULL
            """,
            (tenant_id, session_key),
        )
        row = cur.fetchone()
        if row and row["plan_state"]:
            result: dict[str, Any] = row["plan_state"]
            return result
        return None


def cleanup_stale_sessions(ttl_days: int = 7) -> int:
    """Delete sessions older than TTL. Returns number of sessions deleted."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM chat_sessions
            WHERE last_active_at < NOW() - INTERVAL '%s days'
            """,
            (ttl_days,),
        )
        count = int(cur.rowcount)
        conn.commit()
        return count


# ── Async wrappers (fire-and-forget from event loops) ──


async def save_exchange_async(
    session_key: str,
    user_content: str,
    assistant_content: str,
    channel: str = "telegram",
    model_override: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Non-blocking wrapper around save_exchange. Logs warning on failure.

    Also schedules background embedding of the inserted turns for verbatim
    retrieval via search_chat_turns().
    """
    loop = asyncio.get_running_loop()
    try:
        inserted_ids = await loop.run_in_executor(
            None,
            lambda: save_exchange(
                session_key,
                user_content,
                assistant_content,
                channel=channel,
                model_override=model_override,
                tenant_id=tenant_id,
            ),
        )
    except Exception as e:
        logger.warning("Failed to persist chat exchange for %s: %s", session_key, e)
        return

    if inserted_ids:
        texts = [user_content, assistant_content]
        asyncio.create_task(_embed_turns(inserted_ids, texts))


async def _embed_turns(message_ids: list[int], texts: list[str]) -> None:
    """Background task: embed chat turns and store the vectors.

    Best-effort — failures are logged but never propagate. Embeddings are
    batched in one Ollama call for efficiency.
    """
    if not message_ids:
        return
    try:
        from robothor.llm import ollama as llm_client

        embeddings = await llm_client.get_embeddings_batch_async(texts)
    except Exception as e:
        logger.warning("chat turn embedding failed: %s", e)
        return

    try:
        loop = asyncio.get_running_loop()

        def _update() -> None:
            with get_connection() as conn:
                cur = conn.cursor()
                for mid, emb in zip(message_ids, embeddings, strict=True):
                    if emb is None:
                        continue
                    cur.execute(
                        """
                        UPDATE chat_messages
                        SET embedding = %s, embedded_at = NOW()
                        WHERE id = %s
                        """,
                        (emb, mid),
                    )
                conn.commit()

        await loop.run_in_executor(None, _update)
    except Exception as e:
        logger.warning("chat turn embedding persist failed: %s", e)


def search_chat_turns(
    query_embedding: list[float],
    limit: int = 5,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """Vector search over embedded chat turns scoped to a tenant.

    Returns list of {id, role, content, similarity, created_at, session_key}.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT m.id, m.message, m.created_at,
                   s.session_key,
                   1 - (m.embedding <=> %s::vector) AS similarity
            FROM chat_messages m
            JOIN chat_sessions s ON s.id = m.session_id
            WHERE s.tenant_id = %s
              AND m.embedding IS NOT NULL
            ORDER BY m.embedding <=> %s::vector
            LIMIT %s
            """,
            (query_embedding, tenant_id, query_embedding, limit),
        )
        rows = cur.fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        msg = r["message"] if isinstance(r["message"], dict) else json.loads(r["message"])
        out.append(
            {
                "id": r["id"],
                "role": msg.get("role", "?"),
                "content": msg.get("content", ""),
                "similarity": r["similarity"],
                "created_at": r["created_at"],
                "session_key": r["session_key"],
                "source": "chat_turn",
            }
        )
    return out


def cleanup_stale_chat_turns(days: int = 90, tenant_id: str | None = None) -> int:
    """Delete chat turns older than N days that are (a) not pinned and
    (b) not referenced by any memory_facts.source_content (distilled facts
    always survive). Returns number of turns deleted.

    ``tenant_id`` bounds the sweep to one tenant — nightly maintenance MUST
    pass it explicitly so multi-tenant instances don't delete every tenant's
    data on every run. When ``None`` (manual one-off GC), sweeps globally.

    The NOT EXISTS filter is bounded by tenant + a min-length guard so the
    O(N·M) LIKE scan only fires on substantive messages and stays inside
    the current tenant's fact pool.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if tenant_id is None:
            cur.execute(
                """
                DELETE FROM chat_messages
                WHERE created_at < NOW() - make_interval(days := %s)
                  AND pinned = FALSE
                  AND length(message->>'content') > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_facts f
                      WHERE f.source_content IS NOT NULL
                        AND length(message->>'content') > 20
                        AND f.source_content LIKE '%%' || (message->>'content') || '%%'
                  )
                """,
                (days,),
            )
        else:
            cur.execute(
                """
                DELETE FROM chat_messages
                WHERE tenant_id = %s
                  AND created_at < NOW() - make_interval(days := %s)
                  AND pinned = FALSE
                  AND length(message->>'content') > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_facts f
                      WHERE f.tenant_id = %s
                        AND f.source_content IS NOT NULL
                        AND length(message->>'content') > 20
                        AND f.source_content LIKE '%%' || (message->>'content') || '%%'
                  )
                """,
                (tenant_id, days, tenant_id),
            )
        count = int(cur.rowcount)
        conn.commit()
        return count


async def save_message_async(
    session_key: str,
    role: str,
    content: str,
    channel: str = "telegram",
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Non-blocking wrapper around save_message."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: save_message(
                session_key,
                role,
                content,
                channel=channel,
                tenant_id=tenant_id,
            ),
        )
    except Exception as e:
        logger.warning("Failed to persist chat message for %s: %s", session_key, e)


async def clear_session_async(
    session_key: str,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Non-blocking wrapper around clear_session."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: clear_session(session_key, tenant_id=tenant_id),
        )
    except Exception as e:
        logger.warning("Failed to clear chat session %s: %s", session_key, e)


async def save_plan_state_async(
    session_key: str,
    plan_dict: dict[str, Any],
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Non-blocking wrapper around save_plan_state."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: save_plan_state(session_key, plan_dict, tenant_id=tenant_id),
        )
    except Exception as e:
        logger.warning("Failed to persist plan state for %s: %s", session_key, e)


async def clear_plan_state_async(
    session_key: str,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Non-blocking wrapper around clear_plan_state."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: clear_plan_state(session_key, tenant_id=tenant_id),
        )
    except Exception as e:
        logger.warning("Failed to clear plan state for %s: %s", session_key, e)


async def update_model_override_async(
    session_key: str,
    model_id: str | None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Non-blocking wrapper around update_model_override."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: update_model_override(session_key, model_id, tenant_id=tenant_id),
        )
    except Exception as e:
        logger.warning("Failed to persist model override for %s: %s", session_key, e)

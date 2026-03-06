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

from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "robothor-primary"


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
) -> None:
    """Upsert session + insert user and assistant messages in one transaction."""
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
        for role, content in [("user", user_content), ("assistant", assistant_content)]:
            cur.execute(
                """
                INSERT INTO chat_messages (session_id, message)
                VALUES (%s, %s::jsonb)
                """,
                (session_id, json.dumps({"role": role, "content": content})),
            )

        conn.commit()


def save_message(
    session_key: str,
    role: str,
    content: str,
    channel: str = "telegram",
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Save a single message (used for system injections)."""
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
            """,
            (session_id, json.dumps({"role": role, "content": content})),
        )

        conn.commit()


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
    plan_dict: dict,
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
    """Non-blocking wrapper around save_exchange. Logs warning on failure."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
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
    plan_dict: dict,
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

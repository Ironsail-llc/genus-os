"""
Conversation Session Ingestion for Genus OS Memory System.

Ingests Telegram and webchat conversation sessions into the memory pipeline
so that interactive exchanges compound into the knowledge graph (facts + entities).

Called fire-and-forget after each agent run in telegram.py / chat.py.

Architecture:
    Session history -> dedup check -> split by role -> ingest_content() once per
    role (user turns are user-stated, assistant turns agent-generated; the other
    role is read-only context) -> record
"""

from __future__ import annotations

import logging
import os
from typing import Any

from robothor.engine.sanitize import sanitize_log
from robothor.memory.ingest_state import (
    content_hash,
    get_ingested_count,
    is_already_ingested,
    record_ingested,
)
from robothor.memory.ingestion import ingest_content

logger = logging.getLogger(__name__)

# Minimum number of messages in session history to trigger ingestion.
MIN_HISTORY_THRESHOLD = 4

# Maximum messages to include in the transcript sent to the LLM for extraction.
MAX_TRANSCRIPT_MESSAGES = 20

# Dedup source name used in ingested_items table.
_DEDUP_SOURCE = "conversation_session"
# Per-session message-count watermark (incremental path only).
_WATERMARK_SOURCE = "conversation_session_watermark"

# Heuristic markers for an agent's OWN scheduled output (briefings/summaries).
# Re-ingesting these mistakes the agent re-reporting an event for the event
# recurring — the dominant churn driver. Names are generic feature labels.
_GENERATED_MARKERS = (
    "morning briefing",
    "evening wind-down",
    "evening winddown",
    "daily summary",
    "weekly review",
)


def _ingest_skip_generated_enabled() -> bool:
    """WS-3: skip re-ingesting agent-emitted briefings and extract only NEW turns
    per session (incremental). Default OFF. When on, both reduce the churn where
    the same event is re-extracted from the agent's own repeated output."""
    raw = os.environ.get("MEMORY_INGEST_SKIP_GENERATED", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _is_generated_briefing(content: str) -> bool:
    """True if an assistant turn looks like the agent's own scheduled briefing."""
    head = (content or "").strip()[:240].lower()
    return any(m in head for m in _GENERATED_MARKERS)


def format_transcript(history: list[dict[str, Any]], *, skip_generated: bool = False) -> str:
    """Format session history as a readable transcript for fact extraction.

    Filters out system messages. Truncates to the most recent
    MAX_TRANSCRIPT_MESSAGES entries to bound LLM context usage. When
    ``skip_generated`` is set, assistant turns that are the agent's own
    briefing/summary are dropped so they are not re-extracted as fresh events.
    """
    messages = [m for m in history if m.get("role") in ("user", "assistant")]

    if len(messages) > MAX_TRANSCRIPT_MESSAGES:
        messages = messages[-MAX_TRANSCRIPT_MESSAGES:]

    lines = []
    for msg in messages:
        content = msg.get("content", "")
        if not content:
            continue
        if skip_generated and msg["role"] == "assistant" and _is_generated_briefing(content):
            continue
        role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def role_transcripts(
    history: list[dict[str, Any]], *, skip_generated: bool = False
) -> dict[str, str]:
    """The last MAX_TRANSCRIPT_MESSAGES turns, split by who said them.

    ``{"user": ..., "assistant": ...}``, each in :func:`format_transcript`'s
    line format and order. Extraction runs once per role so a fact's origin is
    its speaker's.
    """
    messages = [m for m in history if m.get("role") in ("user", "assistant")]
    if len(messages) > MAX_TRANSCRIPT_MESSAGES:
        messages = messages[-MAX_TRANSCRIPT_MESSAGES:]
    return {
        role: format_transcript(
            [m for m in messages if m["role"] == role], skip_generated=skip_generated
        )
        for role in ("user", "assistant")
    }


def _merge(total: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    """Two per-role ingest results as one: ids joined, counts added."""
    if not total:
        return dict(part)
    out = dict(total)
    for key, value in part.items():
        if isinstance(value, bool):
            out[key] = bool(out.get(key)) or value
        elif isinstance(value, int):
            out[key] = int(out.get(key) or 0) + value
        elif isinstance(value, list):
            out[key] = list(out.get(key) or []) + value
        elif out.get(key) is None:
            out[key] = value
    return out


def _compute_session_hash(session_key: str, history: list[dict[str, Any]]) -> str:
    """Compute a deterministic hash for dedup."""
    tail = ""
    if history:
        tail = (history[-1].get("content") or "")[:200]

    return content_hash(
        {"key": session_key, "n": str(len(history)), "tail": tail},
        ["key", "n", "tail"],
    )


async def ingest_conversation_session(
    session_key: str,
    history: list[dict[str, Any]],
    agent_id: str,
    trigger_type: str,
    run_id: str,
    tenant_id: str = "",
) -> dict[str, Any] | None:
    """Ingest a conversation session into the memory pipeline.

    Called fire-and-forget after each interactive agent run.
    """
    try:
        if len(history) < MIN_HISTORY_THRESHOLD:
            return None

        incremental = _ingest_skip_generated_enabled()
        tid = tenant_id or None
        hash_val = ""

        if incremental:
            # Extract only the turns added since this session was last ingested,
            # dropping the agent's own briefings — instead of re-extracting the
            # trailing 20 messages every turn (the {n, tail} hash never matched a
            # growing session, so the same tail was re-ingested repeatedly).
            last_n = get_ingested_count(_WATERMARK_SOURCE, session_key, tenant_id=tid)
            window = history[last_n:] if 0 < last_n < len(history) else history
            new_msgs = [m for m in window if m.get("role") in ("user", "assistant")]
            if len(new_msgs) < 2:
                record_ingested(_WATERMARK_SOURCE, session_key, str(len(history)), tenant_id=tid)
                return None
            turns = role_transcripts(window, skip_generated=True)
        else:
            hash_val = _compute_session_hash(session_key, history)
            if is_already_ingested(_DEDUP_SOURCE, session_key, hash_val):
                logger.debug(
                    "Session %s already ingested (hash match), skipping",
                    sanitize_log(session_key),
                )
                return None
            turns = role_transcripts(history)

        if not any(t.strip() for t in turns.values()):
            if incremental:
                record_ingested(_WATERMARK_SOURCE, session_key, str(len(history)), tenant_id=tid)
            return None

        metadata = {
            "session_key": session_key,
            "agent_id": agent_id,
            "run_id": run_id,
            "message_count": len(history),
        }
        # Extracted per role, so each fact's origin is who said it: a person's
        # turns are user-stated, the assistant's agent-generated (even when they
        # paraphrase an email). The other side is shown only as read-only context.
        result: dict[str, Any] = {}
        for role, other in (("user", "assistant"), ("assistant", "user")):
            if not turns[role].strip():
                continue
            part = await ingest_content(
                content=turns[role],
                source_channel=trigger_type,
                content_type="conversation",
                metadata=metadata,
                # Without this the watermarks were tenant-correct while the facts
                # they tracked were written to DEFAULT_TENANT.
                tenant_id=tenant_id,
                role=role,
                read_only_context=turns[other] or None,
            )
            result = _merge(result, part)

        if incremental:
            record_ingested(
                _WATERMARK_SOURCE, session_key, str(len(history)), result.get("fact_ids", []), tid
            )
        else:
            record_ingested(_DEDUP_SOURCE, session_key, hash_val, result.get("fact_ids", []))

        logger.info(
            "Ingested conversation session %s: %d facts, %d entities",
            sanitize_log(session_key),
            result.get("facts_processed", 0),
            result.get("entities_stored", 0),
        )
        return result

    except Exception:
        logger.warning(
            "Conversation ingestion failed for %s", sanitize_log(session_key), exc_info=True
        )
        return None

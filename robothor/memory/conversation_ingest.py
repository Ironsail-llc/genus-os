"""
Conversation Session Ingestion for Genus OS Memory System.

Ingests Telegram and webchat conversation sessions into the memory pipeline
so that interactive exchanges compound into the knowledge graph (facts + entities).

Called fire-and-forget after each agent run in telegram.py / chat.py.

Architecture:
    Session history -> format transcript -> dedup check -> ingest_content() -> record
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
            transcript = format_transcript(window, skip_generated=True)
        else:
            hash_val = _compute_session_hash(session_key, history)
            if is_already_ingested(_DEDUP_SOURCE, session_key, hash_val):
                logger.debug(
                    "Session %s already ingested (hash match), skipping",
                    sanitize_log(session_key),
                )
                return None
            transcript = format_transcript(history)

        if not transcript.strip():
            if incremental:
                record_ingested(_WATERMARK_SOURCE, session_key, str(len(history)), tenant_id=tid)
            return None

        result = await ingest_content(
            content=transcript,
            source_channel=trigger_type,
            content_type="conversation",
            metadata={
                "session_key": session_key,
                "agent_id": agent_id,
                "run_id": run_id,
                "message_count": len(history),
            },
            # Without this the watermarks were tenant-correct while the facts
            # they tracked were written to DEFAULT_TENANT.
            tenant_id=tenant_id,
        )

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

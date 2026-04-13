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
from typing import Any

from robothor.engine.sanitize import sanitize_log
from robothor.memory.ingest_state import content_hash, is_already_ingested, record_ingested
from robothor.memory.ingestion import ingest_content

logger = logging.getLogger(__name__)

# Minimum number of messages in session history to trigger ingestion.
MIN_HISTORY_THRESHOLD = 4

# Maximum messages to include in the transcript sent to the LLM for extraction.
MAX_TRANSCRIPT_MESSAGES = 20

# Dedup source name used in ingested_items table.
_DEDUP_SOURCE = "conversation_session"


def format_transcript(history: list[dict[str, Any]]) -> str:
    """Format session history as a readable transcript for fact extraction.

    Filters out system messages. Truncates to the most recent
    MAX_TRANSCRIPT_MESSAGES entries to bound LLM context usage.
    """
    messages = [m for m in history if m.get("role") in ("user", "assistant")]

    if len(messages) > MAX_TRANSCRIPT_MESSAGES:
        messages = messages[-MAX_TRANSCRIPT_MESSAGES:]

    lines = []
    for msg in messages:
        role = "User" if msg["role"] == "user" else "Assistant"
        content = msg.get("content", "")
        if content:
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

        hash_val = _compute_session_hash(session_key, history)
        if is_already_ingested(_DEDUP_SOURCE, session_key, hash_val):
            logger.debug(
                "Session %s already ingested (hash match), skipping", sanitize_log(session_key)
            )
            return None

        transcript = format_transcript(history)
        if not transcript.strip():
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
        )

        record_ingested(
            _DEDUP_SOURCE,
            session_key,
            hash_val,
            result.get("fact_ids", []),
        )

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

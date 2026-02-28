"""
Context Window Management — prevents unbounded growth in persistent sessions.

Estimates token usage, compresses old messages via LLM summary,
and provides stats for the /context command.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Compression threshold (80K estimated tokens)
COMPRESS_THRESHOLD = 80_000

# Number of recent messages to always keep verbatim
KEEP_RECENT = 20

# Model for compression summaries (cheap, fast)
COMPRESS_MODEL = "gemini/gemini-2.5-flash"


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Fast token estimate: total chars / 4, plus 400 per tool call."""
    total_chars = 0
    tool_call_count = 0

    for msg in messages:
        content = msg.get("content")
        if content:
            total_chars += len(content)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            tool_call_count += len(tool_calls)
            for tc in tool_calls:
                fn = tc.get("function", {})
                total_chars += len(fn.get("arguments", ""))
                total_chars += len(fn.get("name", ""))

    return (total_chars // 4) + (tool_call_count * 400)


async def maybe_compress(
    messages: list[dict[str, Any]],
    models: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compress conversation if above threshold.

    Returns potentially compressed message list. Original list is not modified.

    Strategy:
    - Keep messages[0] (system prompt) always
    - Summarize messages[1:-KEEP_RECENT] via a cheap LLM call
    - Keep last KEEP_RECENT messages verbatim
    - If LLM summary fails, use a static placeholder
    """
    est = estimate_tokens(messages)
    if est < COMPRESS_THRESHOLD:
        return messages

    if len(messages) <= KEEP_RECENT + 1:
        return messages  # Not enough to compress

    logger.info(
        "Compressing context: %d messages, ~%d tokens",
        len(messages),
        est,
    )

    system_msg = messages[0]
    old_messages = messages[1:-KEEP_RECENT]
    recent_messages = messages[-KEEP_RECENT:]

    # Build summary of old messages
    summary = await _summarize_messages(old_messages, models)

    compressed = [
        system_msg,
        {"role": "user", "content": summary},
        {
            "role": "assistant",
            "content": "Understood. I have context from our previous conversation.",
        },
        *recent_messages,
    ]

    new_est = estimate_tokens(compressed)
    logger.info(
        "Compression complete: %d → %d messages, ~%d → ~%d tokens",
        len(messages),
        len(compressed),
        est,
        new_est,
    )

    return compressed


async def _summarize_messages(
    messages: list[dict[str, Any]],
    models: list[str] | None = None,
) -> str:
    """Summarize a list of messages via LLM. Falls back to static placeholder."""
    # Count stats for the fallback message
    msg_count = len(messages)
    char_count = sum(len(m.get("content", "") or "") for m in messages)
    token_est = estimate_tokens(messages)

    fallback = (
        f"[Previous conversation: {msg_count} messages, ~{token_est} tokens, details compressed]"
    )

    # Extract text content for summarization
    text_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content")
        if content and role in ("user", "assistant"):
            # Truncate very long messages
            preview = content[:500] if len(content) > 500 else content
            text_parts.append(f"{role}: {preview}")

    if not text_parts:
        return fallback

    conversation_text = "\n".join(text_parts[-30:])  # Last 30 entries max

    try:
        import litellm

        model = COMPRESS_MODEL
        if models:
            model = models[0]

        response = await litellm.acompletion(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this conversation history in 2-3 paragraphs. "
                        "Focus on key topics discussed, decisions made, and any "
                        "pending items. Be concise."
                    ),
                },
                {"role": "user", "content": conversation_text},
            ],
            temperature=0.1,
            max_tokens=500,
        )

        summary_text = response.choices[0].message.content
        if summary_text:
            return f"[Conversation summary]\n{summary_text}"
    except Exception as e:
        logger.warning("Context compression LLM call failed: %s", e)

    return fallback


def get_context_stats(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Get context window statistics."""
    token_est = estimate_tokens(messages)
    role_counts: dict[str, int] = {}
    for msg in messages:
        role = msg.get("role", "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1

    return {
        "estimated_tokens": token_est,
        "message_count": len(messages),
        "role_counts": role_counts,
        "compress_threshold": COMPRESS_THRESHOLD,
        "usage_pct": round((token_est / COMPRESS_THRESHOLD) * 100, 1),
        "would_compress": token_est >= COMPRESS_THRESHOLD,
    }

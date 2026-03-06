"""
Context Window Management — prevents unbounded growth in persistent sessions.

Estimates token usage, compresses old messages via LLM summary,
and provides stats for the /context command.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Compression threshold (80K estimated tokens)
COMPRESS_THRESHOLD = 80_000

# Drain threshold — compress down to this level to prevent thrashing
DRAIN_THRESHOLD = 60_000

# Number of recent messages to always keep verbatim
KEEP_RECENT = 20

# Model for compression summaries (cheap, fast)
COMPRESS_MODEL = "gemini/gemini-2.5-flash"

# ── Compression hooks ──────────────────────────────────────────────

_pre_compress_hooks: list[Callable] = []
_post_compress_hooks: list[Callable] = []


def register_pre_compress_hook(fn: Callable) -> None:
    """Register a hook called before compression with (messages,)."""
    _pre_compress_hooks.append(fn)


def register_post_compress_hook(fn: Callable) -> None:
    """Register a hook called after compression with (old_messages, compressed, summary)."""
    _post_compress_hooks.append(fn)


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


def _clear_old_tool_results(
    messages: list[dict[str, Any]], keep_last: int = 10
) -> list[dict[str, Any]]:
    """Replace old tool results with placeholders to save tokens."""
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for idx in tool_indices[:-keep_last]:
        content = messages[idx].get("content", "")
        char_count = len(content) if isinstance(content, str) else len(str(content))
        if char_count > 200:
            messages[idx] = {
                **messages[idx],
                "content": f"[tool result: {char_count} chars, cleared]",
            }
    return messages


async def maybe_compress(
    messages: list[dict[str, Any]],
    models: list[str] | None = None,
    threshold: int | None = None,
) -> list[dict[str, Any]]:
    """Compress conversation if above threshold.

    Returns potentially compressed message list. Original list is not modified.

    Args:
        messages: The conversation messages to potentially compress.
        models: Optional list of models (first is used for summarization).
        threshold: Token threshold for compression. Defaults to COMPRESS_THRESHOLD (80K).

    Strategy:
    - Keep messages[0] (system prompt) always
    - Summarize messages[1:-KEEP_RECENT] via a cheap LLM call
    - Keep last KEEP_RECENT messages verbatim
    - If LLM summary fails, use a static placeholder
    """
    compress_at = threshold if threshold is not None else COMPRESS_THRESHOLD
    est = estimate_tokens(messages)
    if est < compress_at:
        return messages

    if len(messages) <= KEEP_RECENT + 1:
        return messages  # Not enough to compress

    logger.info(
        "Compressing context: %d messages, ~%d tokens",
        len(messages),
        est,
    )

    # Pre-compression hooks (extract [REMEMBER] content, etc.)
    for hook in _pre_compress_hooks:
        try:
            hook(messages)
        except Exception as e:
            logger.debug("Pre-compress hook failed: %s", e)

    # First pass: clear old tool results to save tokens without losing structure
    messages = _clear_old_tool_results(list(messages), keep_last=KEEP_RECENT)
    cleared_est = estimate_tokens(messages)
    if cleared_est < DRAIN_THRESHOLD:
        logger.info(
            "Tool result clearing sufficient: ~%d → ~%d tokens",
            est,
            cleared_est,
        )
        return messages

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

    # Post-compression hooks (log stats, persist summaries, etc.)
    for hook in _post_compress_hooks:
        try:
            hook(messages, compressed, summary)
        except Exception as e:
            logger.debug("Post-compress hook failed: %s", e)

    return compressed


async def _summarize_messages(
    messages: list[dict[str, Any]],
    models: list[str] | None = None,
) -> str:
    """Summarize a list of messages via LLM. Falls back to static placeholder."""
    # Count stats for the fallback message
    msg_count = len(messages)
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


# ── Default hooks (always active) ─────────────────────────────────


def _default_pre_compress_hook(messages: list[dict[str, Any]]) -> None:
    """Extract [REMEMBER] tagged content from messages before compression.

    Writes extracted content to the agent's working_context memory block
    so important information survives compression.
    """
    remember_items: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if not content or not isinstance(content, str):
            continue
        # Find [REMEMBER] tagged lines
        for line in content.split("\n"):
            if "[REMEMBER]" in line:
                clean = line.replace("[REMEMBER]", "").strip()
                if clean:
                    remember_items.append(clean)

    if not remember_items:
        return

    try:
        from robothor.memory.blocks import write_block

        # Append to working_context block
        content = "\n".join(f"- {item}" for item in remember_items)
        write_block(
            "working_context",
            content,
            mode="append",
            agent_id="system",
        )
        logger.info("Pre-compress hook: saved %d [REMEMBER] items", len(remember_items))
    except Exception as e:
        logger.debug("Failed to save [REMEMBER] items: %s", e)


def _default_post_compress_hook(
    old_messages: list[dict[str, Any]],
    compressed: list[dict[str, Any]],
    summary: str,
) -> None:
    """Log compression statistics to tracking."""
    try:
        # Just log the compression event — no DB write needed for internal tracking
        old_est = estimate_tokens(old_messages)
        new_est = estimate_tokens(compressed)
        logger.info(
            "Compaction: %d→%d messages, ~%dk→~%dk tokens, summary=%d chars",
            len(old_messages),
            len(compressed),
            old_est // 1000,
            new_est // 1000,
            len(summary),
        )
    except Exception as e:
        logger.debug("Post-compress hook logging failed: %s", e)


# Register default hooks on import
register_pre_compress_hook(_default_pre_compress_hook)
register_post_compress_hook(_default_post_compress_hook)

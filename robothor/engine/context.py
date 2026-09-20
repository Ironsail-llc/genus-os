"""
Context Window Management — prevents unbounded growth in persistent sessions.

Estimates token usage, compresses old messages via LLM summary,
and provides stats for the /context command.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from robothor.engine.reasoning_replay import REASONING_FIELDS

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Compression threshold (80K estimated tokens)
COMPRESS_THRESHOLD = 80_000

# Drain threshold — compress down to this level to prevent thrashing
DRAIN_THRESHOLD = 60_000

# Number of recent messages to always keep verbatim
KEEP_RECENT = 20

# ── Compression hooks ──────────────────────────────────────────────

_pre_compress_hooks: list[Callable[..., Any]] = []
_post_compress_hooks: list[Callable[..., Any]] = []


def register_pre_compress_hook(fn: Callable[..., Any]) -> None:
    """Register a hook called before compression with (messages,)."""
    _pre_compress_hooks.append(fn)


def register_post_compress_hook(fn: Callable[..., Any]) -> None:
    """Register a hook called after compression with (old_messages, compressed, summary)."""
    _post_compress_hooks.append(fn)


# Flat char-equivalent cost of an image block (≈1500 tokens after the /4),
# matching typical vision token accounting. Used so multimodal content is
# sized realistically — and so stripping an image to a text placeholder
# (compaction G7 / Rip 18) produces a *visible* token reduction.
_IMAGE_CHARS_EQUIV = 6000


def _content_chars(content: Any) -> int:
    """Char-equivalent size of a message ``content`` (str OR content-block list).

    Plain string → its length. A content-block list (multimodal) → sum of text
    block lengths + a flat per-image cost. The old code did ``len(content)`` on
    a list, which counted the *number of blocks* (≈2), so text-in-list was
    ignored and a base64 image counted as ~0 — under-sizing vision context.
    """
    if not content:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        chars = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("image_url", "image"):
                    chars += _IMAGE_CHARS_EQUIV
                else:
                    chars += len(block.get("text") or block.get("content") or "")
            else:
                chars += len(str(block))
        return chars
    return len(str(content))


def real_tokenizer_enabled() -> bool:
    """Is the exact tokenizer switched on for this process?

    Public because a caller sizing a CEILING has to know whether
    ``estimate_tokens`` is returning a measurement or a heuristic: only the
    heuristic needs a safety margin on top (see ``context_fit.estimate_for``).
    """
    return _real_tokenizer_enabled()


def _real_tokenizer_enabled() -> bool:
    import os

    return os.environ.get("ROBOTHOR_REAL_TOKENIZER_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def estimate_tokens(messages: list[dict[str, Any]], model: str | None = None) -> int:
    """Token estimate for ``messages``.

    Default is the multimodal-aware char/4 heuristic (+400 per tool call, images
    sized via ``_content_chars``). When a ``model`` is given AND
    ``ROBOTHOR_REAL_TOKENIZER_ENABLED`` is set, uses ``litellm.token_counter``
    for an exact count, falling back to the heuristic if the model is unknown.
    """
    if model and _real_tokenizer_enabled():
        try:
            import litellm

            return int(litellm.token_counter(model=model, messages=messages))
        except Exception as e:
            # Unknown model / counter error → heuristic below. Log it so the
            # real-tokenizer mode doesn't silently degrade with no signal.
            logger.warning(
                "real tokenizer failed for model %s (%s); using char heuristic", model, e
            )

    total_chars = 0
    tool_call_count = 0

    for msg in messages:
        content = msg.get("content")
        if content:
            total_chars += _content_chars(content)
        # Reasoning replayed to a thinking-mode provider is prompt like any
        # other (see reasoning_replay). Uncounted, a multi-turn run's estimate
        # drifts below what is actually sent and the window overruns silently.
        for field in REASONING_FIELDS:
            reasoning = msg.get(field)
            if reasoning:
                total_chars += _content_chars(reasoning)
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
    """Replace old tool results with semantic summaries to save tokens."""
    from robothor.engine.compaction import extract_tool_summary

    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for idx in tool_indices[:-keep_last]:
        content = messages[idx].get("content", "")
        char_count = len(content) if isinstance(content, str) else len(str(content))
        if char_count > 200:
            summary = extract_tool_summary(content if isinstance(content, str) else str(content))
            messages[idx] = {
                **messages[idx],
                "content": f"[tool result: {summary}]",
            }
    return messages


async def maybe_compress(
    messages: list[dict[str, Any]],
    models: list[str] | None = None,
    threshold: int | None = None,
    broken_models: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compress conversation if above threshold.

    Returns potentially compressed message list. Original list is not modified.

    Delegates to the graduated 4-pass compaction system:
    1. Tool result thinning (heuristic summaries)
    2. Structured fact extraction (LLM → retained context)
    3. Segmented LLM summary (chunked, not lossy single-pass)
    4. Progressive pruning (drop oldest summaries, keep facts)

    Args:
        messages: The conversation messages to potentially compress.
        models: Optional list of models (first is used for summarization).
        threshold: Token threshold for compression. Defaults to COMPRESS_THRESHOLD (80K).
    """
    from robothor.engine.compaction import compact

    compress_at = threshold if threshold is not None else COMPRESS_THRESHOLD
    # Offload to a thread: with the real tokenizer enabled this calls
    # litellm.token_counter, a synchronous, potentially CPU-bound call we must
    # not run on the event loop in the compaction hot path.
    from robothor.engine.context_fit import next_reachable_model

    model = next_reachable_model(models or [], broken_models)
    est = await asyncio.to_thread(estimate_tokens, messages, model or None)
    if est < compress_at:
        return messages

    from robothor.engine.context_control import control, fingerprint

    state = control.get()
    if state is not None and state.skip(messages, model, compress_at, est):
        return messages
    started = time.monotonic()
    if (
        state is not None
        and state.active_request
        and not any(str(m.get("content", "")).startswith("[ACTIVE REQUEST]") for m in messages)
    ):
        messages = [
            messages[0],
            {"role": "developer", "content": "[ACTIVE REQUEST]\n" + state.active_request},
            *messages[1:],
        ]

    # The count floor used to live here TOO, and returned before compact()
    # could act — so a 21-message, 225,015-token conversation reduced by 0.0%
    # through this path even after compact() learned to shrink it. One rule,
    # one place: compact() decides, and still leaves a genuinely short
    # exchange alone.

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

    # Delegate to graduated compaction
    result = await compact(
        messages,
        models=models,
        threshold=compress_at,
        drain_to=min(DRAIN_THRESHOLD, max(1, int(compress_at * 0.75))),
        broken_models=broken_models,
    )

    compressed = _restore_output_contract(messages, result.messages)
    # Summarisation is best effort; reaching the target must not depend on it.
    # Remove complete historical exchanges if a long reasoning/tool tail still
    # fills the prompt. Keep provider-required reasoning on retained exchanges.
    from robothor.engine.context_fit import ContextFit, estimate_for, shrink_to_fit

    target = min(DRAIN_THRESHOLD, max(1, int(compress_at * 0.75)))
    if estimate_for(compressed, model) > target:
        outcome = shrink_to_fit(compressed, ContextFit(model, target, target, 0))
        if outcome.tokens_after < outcome.tokens_before:
            compressed = outcome.messages
    after = estimate_for(compressed, model)
    if state is not None:
        state.fingerprint = fingerprint(compressed)
        state.model = model
        state.threshold = compress_at
        state.stalled_tokens = after if after >= compress_at else 0
        state.measurements.append(
            {
                "duration_ms": int((time.monotonic() - started) * 1000),
                "tokens_before": est,
                "tokens_after": after,
                "target_tokens": target,
                "target_reached": after <= target,
            }
        )
    logger.info(
        "Compaction complete: %d → %d messages, ~%d → ~%d tokens, "
        "%d facts extracted, %d passes used",
        len(messages),
        len(compressed),
        result.tokens_before,
        result.tokens_after,
        len(result.facts_extracted),
        result.passes_used,
    )

    # Post-compression hooks (log stats, persist summaries, etc.)
    summary = f"[Compacted: {result.passes_used} passes, {len(result.facts_extracted)} facts]"
    for hook in _post_compress_hooks:
        try:
            hook(messages, compressed, summary)
        except Exception as e:
            logger.debug("Post-compress hook failed: %s", e)

    return compressed


def _restore_output_contract(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Put the task's output requirements back in front of the model.

    ``compaction.protected_prefix_len`` keeps the task statement itself; this is
    the belt to that brace, and it earns its place because the statement is not
    always in the protected head — a resumed run, a long history, a task
    restated mid-conversation. The contract is small, exact, and extracted from
    the PRE-compaction messages, where the spec certainly still is.

    Appended last, so it is the most recent thing the model reads, and only
    once: a conversation that already carries the marker gets nothing.

    Flag-gated on the deliverable contract's own ladder. Failure here is never
    allowed to cost a run its compaction — the messages come back unchanged.
    """
    try:
        from robothor.engine.deliverable_contract import STICKY_MARKER, contract_sticky_block
        from robothor.engine.deliverables import task_text_from
        from robothor.engine.feature_flags import deliverable_contract_mode
        from robothor.engine.session import ENGINE_CONTEXT_ROLE

        if deliverable_contract_mode() == "off":
            return after
        if any(STICKY_MARKER in str(m.get("content", "")) for m in after):
            return after
        block = contract_sticky_block(task_text_from(before))
        if not block:
            return after
        logger.info("Compaction: re-stating the task's output contract (%d chars)", len(block))
        return [*after, {"role": ENGINE_CONTEXT_ROLE, "content": block}]
    except Exception as exc:  # noqa: BLE001 — compaction must not fail on this
        logger.debug("output-contract restore skipped: %s", exc)
        return after


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
        from robothor.memory.blocks import read_block, write_block

        # Append to working_context block
        new_content = "\n".join(f"- {item}" for item in remember_items)
        existing = read_block("working_context")
        old_content = existing.get("content", "") if existing else ""
        combined = f"{old_content}\n{new_content}".strip() if old_content else new_content
        write_block("working_context", combined)
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

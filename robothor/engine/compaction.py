"""
Enhanced Context Compaction — fact-preserving, graduated compression.

4-pass strategy:
1. Tool result thinning — heuristic one-liners for large tool results
2. Structured fact extraction — LLM extracts JSON facts as retained context
3. Segmented LLM summary — chunk old messages, summarize each separately
4. Progressive pruning — drop oldest summaries, keep retained facts

Core idea: extract structured facts BEFORE summarizing. Facts survive all
future compactions via the [RETAINED CONTEXT] marker.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

RETAINED_CONTEXT_MARKER = "[RETAINED CONTEXT]"

# Segment size for chunked summarization (pass 3)
SEGMENT_SIZE = 20

# Minimum tool result length to apply summary extraction
TOOL_SUMMARY_MIN_CHARS = 500

# Model for fact extraction (cheap, fast)
FACT_EXTRACTION_MODEL = "gemini/gemini-2.5-flash"

FACT_EXTRACTION_PROMPT = """\
Extract key facts from this conversation segment. Return JSON only:
{"facts": [
  {"category": "decision", "text": "User decided to use PostgreSQL for vault", "priority": 5},
  {"category": "pending", "text": "Need to update CRON_MAP.md", "priority": 3}
]}

Categories: decision (choices made), preference (user likes/dislikes), \
entity (people/projects/tools mentioned), pending (unfinished items), \
error (problems encountered), context (important background)
Priority: 1=trivial, 3=useful, 5=critical

Only include genuinely important facts. Omit routine tool calls and chatter."""

SEGMENT_SUMMARY_PROMPT = """\
Summarize this conversation segment concisely. Focus on what was discussed, \
decisions made, and outcomes. Be brief — 2-4 sentences max."""


@dataclass
class CompactionFact:
    """A single extracted fact that survives compaction."""

    category: str  # decision, preference, entity, pending, error, context
    text: str
    priority: int  # 1-5 (higher = more important)


@dataclass
class CompactionResult:
    """Result of a compaction operation."""

    messages: list[dict[str, Any]]
    facts_extracted: list[CompactionFact] = field(default_factory=list)
    passes_used: int = 0
    tokens_before: int = 0
    tokens_after: int = 0


def extract_tool_summary(content: str) -> str:
    """Extract a one-line semantic summary from a tool result.

    Heuristic-based (no LLM call). For tool results > TOOL_SUMMARY_MIN_CHARS,
    produces a compact summary preserving the key signal.
    """
    if not content or not isinstance(content, str):
        return content or ""

    if len(content) < TOOL_SUMMARY_MIN_CHARS:
        return content

    stripped = content.strip()

    # Try JSON parsing
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            keys = list(parsed.keys())
            if len(keys) == 1:
                val = parsed[keys[0]]
                val_preview = str(val)[:60]
                return f"{{{keys[0]!r}: {val_preview}{'...' if len(str(val)) > 60 else ''}}}"
            return f"{{{len(keys)} keys: {', '.join(keys[:5])}}}"
        if isinstance(parsed, list):
            preview = str(parsed[0])[:60] if parsed else ""
            return f"[{len(parsed)} items{': ' + preview + '...' if preview else ''}]"
    except (json.JSONDecodeError, TypeError, IndexError):
        pass

    # Error string — first line
    first_line = stripped.split("\n", 1)[0].strip()
    if any(kw in first_line.lower() for kw in ("error", "traceback", "exception", "failed")):
        return first_line[:120]

    # Default: first 80 chars
    return stripped[:80] + ("..." if len(stripped) > 80 else "")


async def extract_facts(
    messages: list[dict[str, Any]],
    model: str = FACT_EXTRACTION_MODEL,
) -> list[CompactionFact]:
    """Extract structured facts from conversation messages via LLM.

    Returns empty list on any failure (never crashes).
    """
    if not messages:
        return []

    # Build conversation text for the LLM
    text_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content")
        if content and isinstance(content, str) and role in ("user", "assistant"):
            preview = content[:300] if len(content) > 300 else content
            text_parts.append(f"{role}: {preview}")

    if not text_parts:
        return []

    conversation_text = "\n".join(text_parts[-40:])  # Last 40 entries max

    try:
        import litellm

        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": FACT_EXTRACTION_PROMPT},
                {"role": "user", "content": conversation_text},
            ],
            temperature=0.1,
            max_tokens=1000,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content
        if not raw:
            return []

        parsed = json.loads(raw)
        raw_facts = parsed.get("facts", [])

        seen: set[str] = set()
        facts: list[CompactionFact] = []
        for f in raw_facts:
            text = f.get("text", "").strip()
            category = f.get("category", "context")
            priority = int(f.get("priority", 3))
            if not text or text in seen:
                continue
            seen.add(text)
            facts.append(
                CompactionFact(category=category, text=text, priority=min(max(priority, 1), 5))
            )

        return facts

    except Exception as e:
        logger.warning("Fact extraction failed: %s", e)
        return []


async def summarize_segment(
    messages: list[dict[str, Any]],
    model: str = FACT_EXTRACTION_MODEL,
) -> str:
    """Summarize a segment of conversation messages via LLM.

    Falls back to a static placeholder on failure.
    """
    from robothor.engine.context import estimate_tokens

    msg_count = len(messages)
    token_est = estimate_tokens(messages)
    fallback = f"[Segment: {msg_count} messages, ~{token_est} tokens, details compressed]"

    if not messages:
        return fallback

    text_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content")
        if content and isinstance(content, str) and role in ("user", "assistant"):
            preview = content[:400] if len(content) > 400 else content
            text_parts.append(f"{role}: {preview}")

    if not text_parts:
        return fallback

    try:
        import litellm

        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": SEGMENT_SUMMARY_PROMPT},
                {"role": "user", "content": "\n".join(text_parts)},
            ],
            temperature=0.1,
            max_tokens=300,
        )

        summary_text: str | None = response.choices[0].message.content
        if summary_text:
            return summary_text.strip()
    except Exception as e:
        logger.warning("Segment summarization failed: %s", e)

    return fallback


def _build_retained_context_message(facts: list[CompactionFact]) -> dict[str, Any]:
    """Build a retained context message from extracted facts."""
    lines = [RETAINED_CONTEXT_MARKER]
    # Sort by priority descending
    lines.extend(
        f"- [{fact.category}] (p{fact.priority}) {fact.text}"
        for fact in sorted(facts, key=lambda f: f.priority, reverse=True)
    )
    return {"role": "user", "content": "\n".join(lines)}


def _is_retained_context(msg: dict[str, Any]) -> bool:
    """Check if a message is a retained context marker."""
    content = msg.get("content", "")
    return isinstance(content, str) and RETAINED_CONTEXT_MARKER in content


def _find_safe_split_index(messages: list[dict[str, Any]], target_idx: int) -> int:
    """Find a split point that never orphans tool_call/tool_result pairs.

    Walks backward from *target_idx* until the boundary sits between two
    independent message groups (not inside an assistant→tool sequence).
    """
    if target_idx <= 0 or target_idx >= len(messages):
        return target_idx

    idx = target_idx
    while idx > 0:
        msg = messages[idx]
        # tool result must stay with its preceding assistant message
        if msg.get("role") == "tool":
            idx -= 1
            continue
        # assistant with tool_calls must stay with the tool results that follow
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            idx -= 1
            continue
        break

    return idx


async def compact(
    messages: list[dict[str, Any]],
    models: list[str] | None = None,
    threshold: int = 80_000,
    drain_to: int = 60_000,
) -> CompactionResult:
    """4-pass graduated compaction.

    Each pass checks if below drain_to before proceeding to the next.
    Retained context messages (facts) are never dropped.

    Args:
        messages: Conversation messages to compact.
        models: Model list (first used for summarization if provided).
        threshold: Token count above which compaction triggers.
        drain_to: Target token count to drain down to.

    Returns:
        CompactionResult with compacted messages and metadata.
    """
    from robothor.engine.context import KEEP_RECENT, estimate_tokens

    tokens_before = estimate_tokens(messages)

    if tokens_before < threshold:
        return CompactionResult(
            messages=messages,
            passes_used=0,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
        )

    if len(messages) <= KEEP_RECENT + 1:
        return CompactionResult(
            messages=messages,
            passes_used=0,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
        )

    summary_model = models[0] if models else FACT_EXTRACTION_MODEL
    working = list(messages)  # Shallow copy
    all_facts: list[CompactionFact] = []

    # ── Pass 1: Tool result thinning ──────────────────────────────────
    tool_indices = [i for i, m in enumerate(working) if m.get("role") == "tool"]
    for idx in tool_indices[:-KEEP_RECENT]:
        content = working[idx].get("content", "")
        char_count = len(content) if isinstance(content, str) else len(str(content))
        if char_count > TOOL_SUMMARY_MIN_CHARS:
            summary = extract_tool_summary(content if isinstance(content, str) else str(content))
            working[idx] = {**working[idx], "content": f"[tool result: {summary}]"}

    est = estimate_tokens(working)
    if est < drain_to:
        logger.info("Pass 1 (tool thinning) sufficient: ~%d → ~%d tokens", tokens_before, est)
        return CompactionResult(
            messages=working,
            passes_used=1,
            tokens_before=tokens_before,
            tokens_after=est,
        )

    # ── Pass 2: Structured fact extraction ────────────────────────────
    system_msg = working[0]

    # Separate retained context messages — they always survive
    retained_msgs = [m for m in working[1:] if _is_retained_context(m)]
    non_retained = [m for m in working[1:] if not _is_retained_context(m)]

    # Split into old and recent (from non-retained messages).
    # Use a safe split point that never orphans tool_call/tool_result pairs.
    if len(non_retained) > KEEP_RECENT:
        split_idx = _find_safe_split_index(non_retained, len(non_retained) - KEEP_RECENT)
        old_messages = non_retained[:split_idx]
        recent_messages = non_retained[split_idx:]
    else:
        old_messages = []
        recent_messages = non_retained

    # Extract facts from old messages
    new_facts = await extract_facts(old_messages, model=summary_model)
    all_facts.extend(new_facts)

    # Merge any facts from previously retained context messages
    for rm in retained_msgs:
        content = rm.get("content", "")
        if isinstance(content, str):
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("- [") and "] (p" in line:
                    # Parse existing retained fact
                    try:
                        cat = line.split("[", 1)[1].split("]", 1)[0]
                        pri = int(line.split("(p", 1)[1].split(")", 1)[0])
                        txt = line.split(") ", 1)[1] if ") " in line else ""
                        if txt:
                            all_facts.append(CompactionFact(category=cat, text=txt, priority=pri))
                    except (IndexError, ValueError):
                        pass

    # Deduplicate facts
    seen: set[str] = set()
    deduped_facts: list[CompactionFact] = []
    for fact in all_facts:
        if fact.text not in seen:
            seen.add(fact.text)
            deduped_facts.append(fact)
    all_facts = deduped_facts

    if not old_messages:
        # Nothing old to summarize — just inject facts and return
        result_msgs = [system_msg]
        if all_facts:
            result_msgs.append(_build_retained_context_message(all_facts))
        result_msgs.extend(recent_messages)
        est = estimate_tokens(result_msgs)
        return CompactionResult(
            messages=result_msgs,
            facts_extracted=all_facts,
            passes_used=2,
            tokens_before=tokens_before,
            tokens_after=est,
        )

    # ── Pass 3: Segmented LLM summary ────────────────────────────────
    # Split old messages into chunks of SEGMENT_SIZE
    segments = [
        old_messages[i : i + SEGMENT_SIZE] for i in range(0, len(old_messages), SEGMENT_SIZE)
    ]

    segment_summaries: list[str] = []
    for segment in segments:
        summary = await summarize_segment(segment, model=summary_model)
        segment_summaries.append(summary)

    # Build compacted message list
    result_msgs = [system_msg]

    # Retained facts always first (after system)
    if all_facts:
        result_msgs.append(_build_retained_context_message(all_facts))

    # Segment summaries as a combined user message
    if segment_summaries:
        combined_summary = "[Conversation summary]\n" + "\n---\n".join(segment_summaries)
        result_msgs.append({"role": "user", "content": combined_summary})
        result_msgs.append(
            {
                "role": "assistant",
                "content": "Understood. I have context from our previous conversation.",
            }
        )

    result_msgs.extend(recent_messages)

    est = estimate_tokens(result_msgs)
    if est < drain_to:
        logger.info("Pass 3 (segmented summary) sufficient: ~%d → ~%d tokens", tokens_before, est)
        return CompactionResult(
            messages=result_msgs,
            facts_extracted=all_facts,
            passes_used=3,
            tokens_before=tokens_before,
            tokens_after=est,
        )

    # ── Pass 4: Progressive pruning ───────────────────────────────────
    # Drop oldest segment summaries, keep facts
    while est >= drain_to and len(segment_summaries) > 1:
        segment_summaries.pop(0)
        result_msgs = [system_msg]
        if all_facts:
            result_msgs.append(_build_retained_context_message(all_facts))
        if segment_summaries:
            combined_summary = "[Conversation summary]\n" + "\n---\n".join(segment_summaries)
            result_msgs.append({"role": "user", "content": combined_summary})
            result_msgs.append(
                {
                    "role": "assistant",
                    "content": "Understood. I have context from our previous conversation.",
                }
            )
        result_msgs.extend(recent_messages)
        est = estimate_tokens(result_msgs)

    logger.info("Pass 4 (progressive pruning): ~%d → ~%d tokens", tokens_before, est)
    return CompactionResult(
        messages=result_msgs,
        facts_extracted=all_facts,
        passes_used=4,
        tokens_before=tokens_before,
        tokens_after=est,
    )

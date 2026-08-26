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

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from robothor.engine.sanitize import sanitize_log

logger = logging.getLogger(__name__)

RETAINED_CONTEXT_MARKER = "[RETAINED CONTEXT]"

# Segment size for chunked summarization (pass 3)
SEGMENT_SIZE = 20

# Minimum tool result length to apply summary extraction
TOOL_SUMMARY_MIN_CHARS = 500

# Model for fact extraction (cheap, fast)
FACT_EXTRACTION_MODEL = "gemini/gemini-2.5-flash"
# Hard ceiling on the compaction LLM call. It runs inside the agent loop, so a
# hung provider (Gemini has been flaky) would otherwise stall the whole run and
# trip the stall watchdog as if the agent itself froze (audit 2026-05-29).
COMPACTION_LLM_TIMEOUT = 45.0

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


#: Per-tool-result budget when feeding fact extraction. Enough for a table
#: or a coordinate dump, small enough that forty of them stay inside the
#: summariser's own window.
TOOL_FACT_CHARS = 1200

#: How many recent tool calls the deterministic tail names.
TAIL_TOOL_CALLS = 8

#: Tools whose `path` argument names something the run produced.
_WRITING_TOOLS = ("write_file", "edit_file", "append_file", "create_file")


def _content_text(content: Any) -> str:
    """Flatten message content to text, dropping image payloads.

    Tool results became content-block lists when image viewing shipped; a
    base64 payload must never be fed to the summariser (it is megabytes of
    noise that would evict the real findings).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        )
    return ""


def build_extraction_input(messages: list[dict[str, Any]]) -> str:
    """The conversation text fact extraction actually reads.

    Tool results used to be excluded by construction — the filter was
    `role in ("user", "assistant")` — so in an agentic run, where the
    findings live in tool output, the summariser kept the agent's prose and
    dropped its evidence. A benchmark run lost its grid dimensions, pitch,
    origins and colour table at compaction and restarted extraction from
    zero at the halfway point of its budget.

    Prose keeps its 300-char preview: it is narration, and the opening is
    the informative part. Tool results get a larger budget through
    `extract_tool_summary`, because truncating a table at 300 characters
    keeps the header and discards the data.
    """
    text_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        text = _content_text(msg.get("content"))
        if not text:
            continue
        if role in ("user", "assistant"):
            text_parts.append(f"{role}: {text[:300]}")
        elif role == "tool":
            summary = extract_tool_summary(text)
            text_parts.append(f"tool: {summary[:TOOL_FACT_CHARS]}")
    return "\n".join(text_parts[-40:])


def deterministic_tail(messages: list[dict[str, Any]]) -> str:
    """Run state that survives compaction without an LLM succeeding.

    Fact extraction is one `gemini-2.5-flash` call with a 1000-token cap and
    a timeout; when it fails or returns thin, everything the run knew is
    gone. The paths a run has written and what it most recently did are
    cheap to derive exactly, so they should never ride on that call.

    Never raises: this runs on the compaction path, and a malformed tool
    call must not cost a run its whole history.
    """
    written: list[str] = []
    recent: list[str] = []
    for msg in messages:
        calls = msg.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "")
            args: dict[str, Any] = {}
            try:
                raw = fn.get("arguments")
                if isinstance(raw, str):
                    args = json.loads(raw)
                elif isinstance(raw, dict):
                    args = raw
            except (ValueError, TypeError):
                args = {}
            if name:
                recent.append(name)
            path = args.get("path") if isinstance(args, dict) else None
            if name in _WRITING_TOOLS and isinstance(path, str) and path not in ("", *written):
                written.append(path)

    lines: list[str] = []
    if written:
        lines.append("Files written this run: " + ", ".join(written[-TAIL_TOOL_CALLS:]))
    if recent:
        lines.append("Recent tool calls: " + ", ".join(recent[-TAIL_TOOL_CALLS:]))
    return "\n".join(lines)[:3500]


async def extract_facts(
    messages: list[dict[str, Any]],
    model: str = FACT_EXTRACTION_MODEL,
) -> list[CompactionFact]:
    """Extract structured facts from conversation messages via LLM.

    Returns empty list on any failure (never crashes).
    """
    if not messages:
        return []

    conversation_text = build_extraction_input(messages)
    if not conversation_text:
        return []

    try:
        import litellm

        response = await asyncio.wait_for(
            litellm.acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": FACT_EXTRACTION_PROMPT},
                    {"role": "user", "content": conversation_text},
                ],
                temperature=0.1,
                max_tokens=1000,
                response_format={"type": "json_object"},
            ),
            timeout=COMPACTION_LLM_TIMEOUT,
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

        response = await asyncio.wait_for(
            litellm.acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": SEGMENT_SUMMARY_PROMPT},
                    {"role": "user", "content": "\n".join(text_parts)},
                ],
                temperature=0.1,
                max_tokens=300,
            ),
            timeout=COMPACTION_LLM_TIMEOUT,
        )

        summary_text: str | None = response.choices[0].message.content
        if summary_text:
            return summary_text.strip()
    except Exception as e:
        logger.warning("Segment summarization failed: %s", e)

    return fallback


def _build_retained_context_message(
    facts: list[CompactionFact],
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a retained context message from extracted facts.

    `messages` adds the deterministic tail — the paths this run wrote and
    what it recently did. Optional so existing callers keep working, but
    every caller inside this module passes it: the facts come from one
    LLM call with a 1000-token cap and a timeout, and when that returns
    thin the run should still know what it produced.
    """
    lines = [RETAINED_CONTEXT_MARKER]
    # Sort by priority descending
    lines.extend(
        f"- [{fact.category}] (p{fact.priority}) {fact.text}"
        for fact in sorted(facts, key=lambda f: f.priority, reverse=True)
    )
    if messages:
        tail = deterministic_tail(messages)
        if tail:
            lines.append(tail)
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


def _dedup_tool_results(
    messages: list[dict[str, Any]], protect_tail: int
) -> tuple[list[dict[str, Any]], int]:
    """Elide repeated identical tool results outside the protected recent tail.

    Reading the same file (or re-running the same command) N times keeps N full
    copies in context. We keep the *newest* full copy (so the agent sees the
    current state) and replace earlier identical ones with a one-line pointer.
    Stateless, lossless-for-the-agent (it can re-fetch), and LLM-free.
    """
    import hashlib

    n = len(messages)
    if n <= protect_tail:
        return messages, 0
    cut = n - protect_tail
    last_seen: dict[str, int] = {}
    for i in range(cut):
        m = messages[i]
        c = m.get("content")
        if m.get("role") == "tool" and isinstance(c, str) and len(c) > 200:
            last_seen[hashlib.sha256(c.encode("utf-8", "ignore")).hexdigest()] = i

    out = list(messages)
    elided = 0
    for i in range(cut):
        m = messages[i]
        c = m.get("content")
        if m.get("role") == "tool" and isinstance(c, str) and len(c) > 200:
            h = hashlib.sha256(c.encode("utf-8", "ignore")).hexdigest()
            if last_seen.get(h) != i:  # an earlier duplicate of a later copy
                out[i] = {**m, "content": "[duplicate of a later identical tool result — elided]"}
                elided += 1
    return out, elided


def _strip_historical_media(
    messages: list[dict[str, Any]], protect_tail: int
) -> tuple[list[dict[str, Any]], int]:
    """Replace image/media blocks with a placeholder outside the recent tail.

    Base64 screenshots dominate token counts and are rarely needed once acted
    on; the recent tail keeps real media so the current turn is unaffected.
    """
    n = len(messages)
    if n <= protect_tail:
        return messages, 0
    cut = n - protect_tail
    out = list(messages)
    stripped = 0
    for i in range(cut):
        m = messages[i]
        content = m.get("content")
        if not isinstance(content, list):
            continue
        new_blocks: list[Any] = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                new_blocks.append({"type": "text", "text": "[image omitted in compaction]"})
                changed = True
                stripped += 1
            else:
                new_blocks.append(block)
        if changed:
            out[i] = {**m, "content": new_blocks}
    return out, stripped


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

    # ── Pass 0: LLM-free pre-pass (Rip 18 / G7) — dedup + strip media ──
    # Cheap, lossless-for-the-agent reclamation before any LLM summary, per the
    # Hermes pattern. Often recovers enough alone to skip the costlier passes.
    from robothor.engine.feature_flags import compaction_hardening_enabled

    if compaction_hardening_enabled():
        from robothor.engine.context import KEEP_RECENT as _KEEP_RECENT

        working, _eli = _dedup_tool_results(working, _KEEP_RECENT)
        working, _med = _strip_historical_media(working, _KEEP_RECENT)
        if _eli or _med:
            logger.info(
                "Compaction pre-pass: elided %s duplicate tool results, stripped %s media blocks",
                sanitize_log(_eli),
                sanitize_log(_med),
            )
            est0 = estimate_tokens(working)
            if est0 < drain_to:
                logger.info(
                    "Pre-pass (dedup/media) sufficient: ~%d → ~%d tokens", tokens_before, est0
                )
                return CompactionResult(
                    messages=working,
                    passes_used=1,
                    tokens_before=tokens_before,
                    tokens_after=est0,
                )

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
            result_msgs.append(_build_retained_context_message(all_facts, messages))
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
        result_msgs.append(_build_retained_context_message(all_facts, messages))

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
            result_msgs.append(_build_retained_context_message(all_facts, messages))
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

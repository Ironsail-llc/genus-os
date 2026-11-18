"""
Context compaction — fact-preserving, on the agent's own model chain.

1. LLM-free pre-pass — dedupe repeated tool results, strip historical media,
   elide oversized old tool results, truncate oversized argument JSON
2. Iterative summary — the previous summary plus the new slice, folded chunk
   by chunk, on the agent's chain (the fleet chain when the caller has none)
3. Structured fact extraction — JSON facts kept as retained context
4. Freeze on failure — when every model fails, the messages are kept and the
   result is marked degraded instead of dropping the middle

Facts survive all future compactions via the [RETAINED CONTEXT] marker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

# Dial through the credential pool. A direct litellm call lets the SDK
# resolve the provider key from the environment, which on 2026-08-27 meant
# this path kept hammering a credential the pool had already retired and
# could not rotate to a spare. No-op for unpooled providers.
from robothor.engine.llm_attempts import (
    REASONING_ONLY_NUDGE,
    describe_completion,
)

# One thinking-kwargs builder for the whole engine: the agent loop's re-ask and
# this one must put the same provider-aware payload on the wire.
from robothor.engine.llm_client import thinking_kwargs_for_call
from robothor.engine.pooled_completion import acompletion as pooled_acompletion
from robothor.engine.sanitize import sanitize_log

logger = logging.getLogger(__name__)

RETAINED_CONTEXT_MARKER = "[RETAINED CONTEXT]"

# Minimum tool result length to apply summary extraction
TOOL_SUMMARY_MIN_CHARS = 500

# Hard ceiling on the compaction LLM call. It runs inside the agent loop, so a
# hung provider (Gemini has been flaky) would otherwise stall the whole run and
# trip the stall watchdog as if the agent itself froze (audit 2026-05-29).
COMPACTION_LLM_TIMEOUT = 45.0

#: On-device generation is far slower than a cloud call, and the engine gives
#: this model class 600s (LLM_REQUEST_TIMEOUT_OLLAMA). A 45s cap on the last
#: link means the chain walk arrives at the offline tier and cancels it —
#: reaching a model you then kill is not reaching it.
COMPACTION_LOCAL_LLM_TIMEOUT = 300.0

#: Ceiling for the whole walk. Per-model timeouts alone multiply by chain
#: length, and compaction runs inside a run's wall-clock budget.
COMPACTION_WALK_BUDGET = 360.0


def _timeout_for(model: str) -> float:
    """Per-model ceiling: the offline tier needs one it can actually meet."""
    return (
        COMPACTION_LOCAL_LLM_TIMEOUT
        if model.startswith(("ollama_chat/", "ollama/"))
        else COMPACTION_LLM_TIMEOUT
    )


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
    #: The context was over budget and no model could summarise it, so the
    #: messages were kept (freeze-on-failure) rather than dropped.
    degraded: bool = False


def extract_tool_summary(content: str) -> str:
    """Extract a one-line semantic summary from a tool result.

    Heuristic-based (no LLM call). For tool results > TOOL_SUMMARY_MIN_CHARS,
    produces a compact summary preserving the key signal.

    Dict results include actual value previews (not just key names) so the
    agent can see there is real data and knows to read_file the full result
    when needed.  This fixes the "recursive chain of {2-3 keys} summaries"
    problem where summaries showed only field names with no values.
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
            # Show up to 3 key-value pairs so the agent sees actual data
            parts = []
            for k in keys[:3]:
                val = str(parsed[k])
                val_preview = val[:40] + ("..." if len(val) > 40 else "")
                parts.append(f"{k}: {val_preview}")
            preview = ", ".join(parts)
            if len(keys) > 3:
                return f"{{{preview}, +{len(keys) - 3} more keys}}"
            return f"{{{preview}}}"
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


async def _re_ask_for_the_answer(model: str, kwargs: dict[str, Any], started: float) -> Any:
    """One same-model re-ask after a reasoning-only reply, or None.

    Asks for less thinking and adds a nudge for the answer itself: an identical
    re-roll reproduces an identical truncation. Every failure returns None so
    the caller falls back to the chain walk it would have done anyway — this
    can only save a walk, never cause one. "Less thinking" is built by the same
    provider-aware helper the agent loop's own re-ask uses
    (``llm_client.thinking_kwargs_for_call``, ``reduced=True``) rather than a
    top-level ``reasoning_effort``: litellm maps that knob per route and maps
    nothing for ``openrouter/xiaomi/mimo-v2.5``, so on the fleet primary every
    re-ask raised ``UnsupportedParamsError`` before it left the process and
    this function returned None into the walk it exists to prevent — a
    30-character summary of an 81k-token context (journal 2026-09-13 19:45 ET).
    It happened to work on DeepSeek, which litellm does map, so the defect read
    as a model quirk. Sharing the builder also means a model registered without
    reasoning support is simply sent no thinking kwargs at all.
    """
    remaining = COMPACTION_WALK_BUDGET - (time.monotonic() - started)
    if remaining <= 0:
        return None
    messages = [*kwargs.get("messages", []), {"role": "user", "content": REASONING_ONLY_NUDGE}]
    # Spread LAST: for the Anthropic family the block carries the temperature
    # that family requires alongside thinking, and it must win over the 0.1
    # every compaction call site passes.
    thinking = thinking_kwargs_for_call(model, int(kwargs.get("max_tokens") or 0), reduced=True)
    try:
        response = await asyncio.wait_for(
            pooled_acompletion(
                model=model,
                **{**kwargs, "messages": messages, **thinking},
            ),
            timeout=min(_timeout_for(model), remaining),
        )
    except Exception as e:  # noqa: BLE001 - the chain walk is the fallback
        logger.warning(
            "Compaction re-ask of %s failed (%s) — falling through to the chain",
            sanitize_log(model),
            sanitize_log(str(e)[:100]),
        )
        return None
    if describe_completion(response).no_answer:
        return None
    logger.info(
        "Compaction model %s answered on the re-ask — chain not walked", sanitize_log(model)
    )
    return response


async def _acompletion_over_chain(models: str | list[str], **kwargs: Any) -> Any:
    """Call the first model in the chain that answers.

    Compaction used to take ``models[0]`` and call litellm directly, so it had
    none of the fallback the agent loop has. During the 2026-08-26 outage that
    meant every extraction went to the capped primary and returned nothing,
    while the local tier answering the agent's own turns sat next in the same
    list. Both legs here swallow failure — extraction returns ``[]`` and the
    summary degrades to a placeholder — so the loss is silent: the agent's
    context is replaced by "[Segment: N messages]" with zero retained facts.
    """

    chain = [models] if isinstance(models, str) else list(models)
    last: Exception | None = None
    started = time.monotonic()
    for model in chain:
        remaining = COMPACTION_WALK_BUDGET - (time.monotonic() - started)
        if remaining <= 0:
            logger.warning("Compaction walk budget exhausted before %s", model)
            break
        try:
            response = await asyncio.wait_for(
                pooled_acompletion(model=model, **kwargs),
                timeout=min(_timeout_for(model), remaining),
            )
        except Exception as e:  # noqa: PERF203 - the walk IS the error handling
            last = e
            logger.warning(
                "Compaction model %s failed (%s) — trying the next in the chain",
                model,
                str(e)[:100],
            )
            continue
        # An empty answer is a failure, not a result. Both callers treat empty
        # as "nothing to retain" and move on silently, so stopping here would
        # lose the context just as thoroughly as an exception — without even
        # trying the tier below.
        shape = describe_completion(response)
        if not shape.no_answer:
            return response
        # …unless the model reasoned and simply never reached the summary. That
        # is a budget problem, not a dead model, and walking the chain for it
        # cost 61 extractions a day — one measured walk ended at the local 27B
        # tier and returned a 30-character summary of an 81k-token context
        # (DIAG 2026-09-13 §1.5, §4.1). Re-ask THIS model for less thinking.
        if shape.reasoning_only:
            retried = await _re_ask_for_the_answer(model, kwargs, started)
            if retried is not None:
                return retried
        last = last or RuntimeError(f"{model} returned no content ({shape.describe()})")
        logger.warning(
            "Compaction model %s returned %s (%s) — trying the next",
            sanitize_log(model),
            shape.outcome,
            sanitize_log(shape.describe()),
        )
    raise last if last is not None else RuntimeError("no compaction model configured")


async def extract_facts(
    messages: list[dict[str, Any]],
    model: str | list[str],
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
        response = await _acompletion_over_chain(
            model,
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


#: The engine's own pin. A message carries it only because engine code put it
#: there — unlike a text prefix, which a model turn or a tool result can type
#: for itself and so was never evidence of anything. Stripped from every
#: provider payload by ``context_control.strip_engine_keys``.
PIN_KEY = "_pin"
ACTIVE_REQUEST_PIN = "active_request"


#: What the operator added while the run worked (live_inbox.py). It amends the
#: request, so it is kept the same way the request is.
FOLLOWUP_PIN = "live_followup"


def _is_pinned(msg: dict[str, Any]) -> bool:
    """Did the ENGINE pin this message as the request, or an addition to it?"""
    return msg.get(PIN_KEY) in (ACTIVE_REQUEST_PIN, FOLLOWUP_PIN)


def _is_retained_context(msg: dict[str, Any]) -> bool:
    """Check if a message is a retained context marker."""
    content = msg.get("content", "")
    return isinstance(content, str) and RETAINED_CONTEXT_MARKER in content


def _in_tool_exchange(messages: list[dict[str, Any]]) -> list[bool]:
    """Flag every message that sits inside an assistant-call → tool-result span.

    A pin cannot be hoisted out of one of these: moving the assistant turn
    leaves its results orphaned, and moving a result leaves the call dangling.
    Providers reject both.
    """
    flags = [False] * len(messages)
    pending: set[str] = set()
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            pending.discard(str(msg.get("tool_call_id")))
            flags[i] = True
            continue
        if pending:
            flags[i] = True
        calls = msg.get("tool_calls")
        if calls:
            flags[i] = True
            pending = {str(c.get("id")) for c in calls}
    return flags


#: Fallback when the settings read fails. Compaction must never break on config.
DEFAULT_PROTECT_FIRST_N = 3


def _split_for_summary(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The pinned head, the retained-context messages, and everything else.

    Extracted from ``compact`` because the function-size ratchet asked for it:
    the head is now a slice rather than ``messages[0]``, and the arithmetic for
    where it ends belongs beside ``protected_prefix_len`` rather than inside a
    276-line function.
    """
    head_len = protected_prefix_len(messages)
    tail = messages[head_len:]
    # Hoist the engine's pinned request into the protected head — but never out
    # of a tool exchange. A pin that sits inside one stays where it is; it is
    # already protected by its own exchange staying whole.
    inside = _in_tool_exchange(tail)
    hoisted = {i for i, m in enumerate(tail) if _is_pinned(m) and not inside[i]}
    active = [m for i, m in enumerate(tail) if i in hoisted]
    rest = [m for i, m in enumerate(tail) if i not in hoisted]
    return (
        [*messages[:head_len], *active],
        [m for m in rest if _is_retained_context(m)],
        [m for m in rest if not _is_retained_context(m)],
    )


def protected_prefix_len(messages: list[dict[str, Any]], protect_first_n: int | None = None) -> int:
    """How many messages at the head compaction may never summarise away.

    MEASURED 2026-09-16 across ten benchmark runs on one model: the four whose
    transcript began with a compaction summary scored a mean of 0.016, the six
    that never compacted 0.450. The required output header appears three times
    in the competing scaffold's context for the worst of them and ZERO times in
    ours — the spec had been summarised away, and the agent then invented the
    deliverable's shape. The competing scaffold compacts harder than we do and
    is immune, on one constant: it pins the first three messages.

    At least 1, because the system prompt was already unconditionally
    protected and turning this setting off must not take that away.

    Never ending on an assistant turn that called a tool: protecting the CALL
    while its RESULT is summarised away leaves a dangling tool_call, which
    several providers reject outright. The prefix shrinks instead.
    """
    if not messages:
        return 0
    if protect_first_n is None:
        from robothor.settings import get_settings

        try:
            protect_first_n = int(get_settings().providers.compaction_protect_first_n)
        except Exception:  # noqa: BLE001 — compaction must never fail on config
            protect_first_n = DEFAULT_PROTECT_FIRST_N
    n = max(1, min(int(protect_first_n or 0), len(messages)))
    inside = _in_tool_exchange(messages)
    # Nor inside one: an engine notice can sit between a call and its results,
    # and a head ending on it keeps the call while the results are dropped.
    while n > 1 and (
        messages[n - 1].get("tool_calls")
        or (n < len(messages) and inside[n] and not messages[n].get("tool_calls"))
    ):
        n -= 1
    return n


def _find_safe_split_index(messages: list[dict[str, Any]], target_idx: int) -> int:
    """Find a split point that never orphans tool_call/tool_result pairs.

    Walks backward from *target_idx* until the boundary sits between two
    independent message groups. An exchange is everything from the assistant
    tool-call message to its last matching result, whatever sits in between:
    an engine notice recorded mid-batch once made this stop at the notice and
    keep the results while their call was dropped, and every provider rejects
    that request.
    """
    if target_idx <= 0 or target_idx >= len(messages):
        return target_idx

    inside = _in_tool_exchange(messages)
    idx = target_idx
    # The boundary BEFORE a call retains both the call and its results.
    # Walking past it joins consecutive exchanges into one unbounded tail.
    while idx > 0 and inside[idx] and not messages[idx].get("tool_calls"):
        idx -= 1
    return idx


def _recent_split(
    messages: list[dict[str, Any]], budget: int, keep_recent: int, model: str | None = None
) -> int:
    """Retain a token-bounded tail, cutting only before complete exchanges."""
    from robothor.engine.context import estimate_context_tokens

    def estimate_tokens(msgs: list[dict[str, Any]]) -> int:
        return estimate_context_tokens(msgs, model)

    start = max(0, len(messages) - keep_recent)
    if start < len(messages) - 1:
        # Incremental, for the same reason ``drain_history`` is: re-summing the
        # whole surviving window each step is a pass over the content per step.
        remaining = estimate_tokens(messages[start:])
        while start < len(messages) - 1 and remaining > budget:
            remaining -= estimate_tokens([messages[start]])
            start += 1
    return _find_safe_split_index(messages, start)


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


#: Messages at the tail left untouched by the token-bound fallback. The agent
#: needs its immediate context verbatim; everything older is a candidate.
SHRINK_KEEP_INTACT = 4

#: How much of an oversized message survives, in characters. Enough to keep the
#: shape and the first rows of a tool result recognisable.
SHRINK_HEAD_CHARS = 2_000


def _shrink_largest(
    messages: list[dict[str, Any]],
    drain_to: int,
    keep_intact: int = SHRINK_KEEP_INTACT,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Truncate the biggest old messages until the conversation fits.

    The last resort when the count-based passes cannot run. It shrinks message
    CONTENT and never removes a message, because dropping one would orphan a
    tool_call/tool_result pair — the corruption `_validate_tool_pairs` exists
    to clean up after.

    Lossy and explicit. That is the point: the alternative is a conversation
    handed to a server that allocated a third of the room it needs, and
    truncated there in silence.
    """
    from robothor.engine.context import estimate_context_tokens

    def estimate_tokens(msgs: list[dict[str, Any]]) -> int:
        return estimate_context_tokens(msgs, model)

    out = list(messages)
    tail_start = max(0, len(out) - keep_intact)
    # Biggest first: one 750KB tool result is worth more than twenty small ones.
    from robothor.engine.compaction_ledger import is_ledger

    order = sorted(
        (i for i in range(1, tail_start) if not is_ledger(out[i])),
        key=lambda i: len(_content_text(out[i].get("content"))),
        reverse=True,
    )
    shrunk = 0
    for i in order:
        if estimate_tokens(out) <= drain_to:
            break
        text = _content_text(out[i].get("content"))
        if len(text) <= SHRINK_HEAD_CHARS:
            continue
        out[i] = {
            **out[i],
            "content": (
                text[:SHRINK_HEAD_CHARS]
                + f"\n\n[... {len(text) - SHRINK_HEAD_CHARS:,} characters elided by compaction —"
                " this result was too large to keep in full]"
            ),
        }
        shrunk += 1
    return out, shrunk


#: Old tool results longer than this become a one-line summary plus a reference.
TOOL_RESULT_MAX_CHARS = 2_000

#: Tool-call argument JSON longer than this is truncated (kept valid JSON).
ARGUMENT_MAX_CHARS = 1_000

#: How much of one long argument string survives truncation.
ARGUMENT_VALUE_CHARS = 200

#: Ceiling on the conversation slice handed to one summary update.
SLICE_MAX_CHARS = 32_000

SUMMARY_MARKER = "[Conversation summary]"

#: Messages one fact-extraction call reads in full (`build_extraction_input`
#: keeps the last 40 parts, one per message at most).
FACT_INPUT_MESSAGES = 40

ITERATIVE_SUMMARY_PROMPT = """You keep the running summary of a long agent run. Update the previous summary \
with the new conversation slice: keep decisions, findings, numbers, file paths \
and unfinished work; drop what the slice supersedes. Return only the updated \
summary, at most 300 words."""


#: The marker a stub's excerpt follows. Also how a later pass finds how much
#: of the excerpt budget earlier stubs already spent.
STUB_PREFIX = "[tool result elided by compaction"
EXCERPT_MARKER = "\nExcerpt:\n"

#: Excerpt length bounds for one stub, in characters. The budget for all of a
#: conversation's excerpts is a twelfth of the drain target; each new stub takes a
#: quarter of what is left, so the first ones keep the most and the total stays
#: bounded however many results are elided. The floor keeps every stub useful.
EXCERPT_MAX_CHARS = 1_000
EXCERPT_MIN_CHARS = 120
_EXCERPT_CUT = "\n[... {n:,} characters removed ...]\n"


def _call_details(messages: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """``tool_call_id`` -> (tool name, argument JSON) for every call in *messages*."""
    details: dict[str, tuple[str, str]] = {}
    for m in messages:
        for call in m.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and call.get("id"):
                raw = fn.get("arguments")
                args = raw if isinstance(raw, str) else json.dumps(raw or {})
                details[str(call["id"])] = (str(fn.get("name") or "the tool"), args)
    return details


def _excerpt(content: str, chars: int) -> str:
    """Head and tail of *content* within *chars*: two thirds head, where a
    result's identifying fields usually are, one third tail."""
    if len(content) <= chars:
        return content
    head = (chars * 2) // 3
    tail = chars - head
    return content[:head] + _EXCERPT_CUT.format(n=len(content) - head - tail) + content[-tail:]


def _stub(name: str, args: str, call_id: str, content: str, excerpt_chars: int) -> str:
    """What the model sees in place of a result: that most of it was removed,
    how to get it back, and a bounded head/tail excerpt. Kept short: a long
    turn can carry dozens of these."""
    shown_args = args if len(args) <= 120 else args[:120] + "..."
    return (
        f"{STUB_PREFIX}: {name} {shown_args} returned {len(content):,} chars (ref {call_id}); "
        f"removed to save space. Before relying on a detail not in this excerpt, call "
        f"{name} again with the same arguments, if it only reads data.]"
        f"{EXCERPT_MARKER}{_excerpt(content, excerpt_chars)}"
    )


def _excerpt_chars_spent(messages: list[dict[str, Any]]) -> int:
    """Excerpt characters already in context: in stubs and in the ledger.

    The ledger counts too. Without it every fold reset the budget, each new
    stub took the largest excerpt again, and the ledger grew until its oldest
    excerpts, the ones a long task started from, had to be cut.
    """
    from robothor.engine.compaction_ledger import ledger_excerpt_chars

    spent = 0
    for m in messages:
        content = m.get("content")
        if m.get("role") == "tool" and isinstance(content, str) and content.startswith(STUB_PREFIX):
            _, _, excerpt = content.partition(EXCERPT_MARKER)
            spent += len(excerpt)
        else:
            spent += ledger_excerpt_chars(m)
    return spent


def _elide_large_tool_results(
    messages: list[dict[str, Any]],
    protect_tail: int,
    max_chars: int = TOOL_RESULT_MAX_CHARS,
    *,
    over_by: int | None = None,
    excerpt_budget: int = 6_000,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Replace old oversized tool results with a stub: what it was, how to get
    it back, and a bounded head/tail excerpt.

    Largest first, oldest first among equals, and only until the conversation
    has shed *over_by* tokens (every candidate when it is None). A turn just
    over its target loses one result, not all of them: measured in a lab run,
    the result that held the fact the final answer needed went with the rest.

    Content only, so a call stays paired with its result. A stub is shorter
    than *max_chars* and deterministic, so it is never rewritten by a later
    pass: the prompt-cache prefix up to it stays byte-identical.
    """
    from robothor.engine.context import estimate_context_tokens

    details = _call_details(messages)
    cut = max(0, len(messages) - protect_tail)
    candidates = [
        i
        for i in range(cut)
        if messages[i].get("role") == "tool"
        and isinstance(messages[i].get("content"), str)
        and len(messages[i]["content"]) > max_chars
    ]
    candidates.sort(key=lambda i: (-len(messages[i]["content"]), i))
    left = max(0, excerpt_budget - _excerpt_chars_spent(messages))
    out = list(messages)
    elided = 0
    shed = 0
    for i in candidates:
        if over_by is not None and shed >= over_by:
            break
        m = messages[i]
        content = m["content"]
        call_id = str(m.get("tool_call_id", "?"))
        name, args = details.get(call_id, ("the tool", "{}"))
        chars = max(EXCERPT_MIN_CHARS, min(EXCERPT_MAX_CHARS, left // 4))
        stub = _stub(name, args, call_id, content, chars)
        if len(stub) >= max_chars:  # never a candidate again, whatever the arguments
            stub = stub[: max_chars - 1]
        left = max(0, left - chars)
        replaced = {**m, "content": stub}
        if over_by is not None:
            shed += estimate_context_tokens([m], model) - estimate_context_tokens([replaced], model)
        out[i] = replaced
        elided += 1
    return out, elided


def newest_exchange_start(messages: list[dict[str, Any]]) -> int:
    """Index of the newest assistant tool call: the results the model has not
    read yet start after it. ``len - 1`` when there is no call at all."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("tool_calls"):
            return i
    return max(0, len(messages) - 1)


def protected_tail_len(
    messages: list[dict[str, Any]], budget: int, cap: int, model: str | None = None
) -> int:
    """How many trailing messages the pre-pass keeps verbatim.

    Bounded by TOKENS as well as by count. A fixed count of recent messages
    (twenty) is right when they are small, and wrong for one turn of large tool
    results: twenty messages there is ten results, the whole turn, and nothing
    could ever be elided — measured, a single tool-heavy turn grew to 900k
    tokens through a compaction that fired every iteration and reduced nothing.

    The newest exchange is always kept whole, whatever it costs: the model
    asked for those results and has not seen them. Older messages join the
    tail newest-first while they fit in *budget*, up to *cap* messages.
    """
    from robothor.engine.context import estimate_context_tokens

    if not messages:
        return 0
    start = newest_exchange_start(messages)
    used = estimate_context_tokens(messages[start:], model)
    while start > 0 and len(messages) - start < cap:
        cost = estimate_context_tokens([messages[start - 1]], model)
        if used + cost > budget:
            break
        used += cost
        start -= 1
    return len(messages) - start


def _truncate_arguments(raw: str, max_chars: int) -> str:
    """Shorten one call's argument JSON and keep it parseable."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        short = {
            k: (
                v[:ARGUMENT_VALUE_CHARS] + f"... [{len(v):,} chars elided by compaction]"
                if isinstance(v, str) and len(v) > ARGUMENT_VALUE_CHARS
                else v
            )
            for k, v in parsed.items()
        }
        text = json.dumps(short)
        if len(text) <= max_chars:
            return text
    return json.dumps({"_elided_by_compaction": raw[: max_chars // 2], "_original_chars": len(raw)})


def _truncate_tool_arguments(
    messages: list[dict[str, Any]], protect_tail: int, max_chars: int = ARGUMENT_MAX_CHARS
) -> tuple[list[dict[str, Any]], int]:
    """Truncate oversized tool-call argument JSON outside the recent tail.

    A ``write_file`` call carries the whole file it wrote, and every later turn
    re-sends it. Copy-on-write: the session's own dicts are never mutated.
    """
    cut = len(messages) - protect_tail
    out = list(messages)
    truncated = 0
    for i in range(max(0, cut)):
        calls = messages[i].get("tool_calls")
        if not isinstance(calls, list):
            continue
        new_calls: list[Any] = []
        changed = False
        for call in calls:
            fn = call.get("function") if isinstance(call, dict) else None
            raw = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(fn, dict) and isinstance(raw, str) and len(raw) > max_chars:
                new_calls.append(
                    {**call, "function": {**fn, "arguments": _truncate_arguments(raw, max_chars)}}
                )
                changed = True
                truncated += 1
            else:
                new_calls.append(call)
        if changed:
            out[i] = {**messages[i], "tool_calls": new_calls}
    return out, truncated


def llm_free_pre_pass(
    messages: list[dict[str, Any]],
    protect_tail: int,
    *,
    target: int | None = None,
    model: str | None = None,
    elide_all: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Reclaim tokens with no model call: the pass that runs first.

    Never touches the protected head (the task statement) or the recent tail.
    Dedupe identical tool results, strip historical media, truncate oversized
    argument JSON, then elide oversized old tool results — in that order, so
    the dedupe compares the originals and elision, the lossy step, comes last.
    With a *target* (tokens), elision stops as soon as the conversation fits it
    (sizing the excerpt budget from it); without one, or with *elide_all*, every
    candidate is elided.
    """
    from robothor.engine.context import estimate_context_tokens

    head_len = protected_prefix_len(messages)
    body = messages[head_len:]
    body, deduped = _dedup_tool_results(body, protect_tail)
    body, media = _strip_historical_media(body, protect_tail)
    body, args = _truncate_tool_arguments(body, protect_tail)
    over_by = None
    budget = 6_000
    if target is not None:
        whole = [*messages[:head_len], *body]
        over_by = None if elide_all else max(0, estimate_context_tokens(whole, model) - target)
        budget = max(EXCERPT_MIN_CHARS * 4, target // 12)
    body, elided = (
        _elide_large_tool_results(
            body, protect_tail, over_by=over_by, excerpt_budget=budget, model=model
        )
        if over_by != 0
        else (body, 0)
    )
    counts = {"deduped": deduped, "media": media, "elided": elided, "arguments": args}
    return [*messages[:head_len], *body], counts


def _is_summary(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    return (
        msg.get("role") == "user"
        and isinstance(content, str)
        and content.startswith(SUMMARY_MARKER)
    )


def _take_previous_summary(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """The previous compaction's summary text, and the messages without it.

    The acknowledgement turn the summary was paired with goes too.
    """
    previous: list[str] = []
    rest: list[dict[str, Any]] = []
    skip_ack = False
    for msg in messages:
        if _is_summary(msg):
            previous.append(str(msg["content"])[len(SUMMARY_MARKER) :].strip())
            skip_ack = True
            continue
        if (
            skip_ack
            and msg.get("role") == "assistant"
            and not msg.get("tool_calls")
            and str(msg.get("content") or "").startswith("Understood")
        ):
            skip_ack = False
            continue
        skip_ack = False
        rest.append(msg)
    return "\n".join(p for p in previous if p), rest


def _slice_chunks(messages: list[dict[str, Any]], cap: int = SLICE_MAX_CHARS) -> list[str]:
    """The slice as summary input, split on message boundaries into chunks of
    at most ``cap`` characters, oldest first.

    Every message lands in exactly one chunk: the caller folds them all into
    the summary, so nothing between the head and the cut leaves context
    unsummarised. One part is a prose preview or a tool-result summary, each
    far under the cap.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for msg in messages:
        role = msg.get("role", "unknown")
        text = _content_text(msg.get("content"))
        if not text:
            continue
        if role in ("user", "assistant"):
            part = f"{role}: {text[:400]}"
        elif role == "tool":
            part = f"tool: {extract_tool_summary(text)[:TOOL_FACT_CHARS]}"
        else:
            continue
        if current and size + len(part) + 1 > cap:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(part)
        size += len(part) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _summary_chain(
    models: list[str] | None, broken_models: set[str] | None, preferred_model: str | None
) -> list[str]:
    """The agent's chain, minus the dead, with its compaction model first.

    No chain from the caller means the fleet's default chain, never a model
    the platform picked: the deployment authorised its chain, not ours.
    """
    chain = list(models or [])
    if not chain:
        from robothor.engine import config as engine_config

        try:
            # Reads the manifest directory: off the loop.
            chain = list(await asyncio.to_thread(engine_config.fleet_model_chain))
        except Exception as e:  # noqa: BLE001 — no chain is reported by the caller
            logger.warning("Compaction: fleet chain unreadable (%s)", sanitize_log(str(e)))
            chain = []
    usable = [m for m in chain if m not in (broken_models or set())] or chain
    if preferred_model and preferred_model in usable:
        usable = [preferred_model, *(m for m in usable if m != preferred_model)]
    return usable


async def update_summary(previous: str, slice_text: str, chain: list[str]) -> str:
    """One call: the previous summary plus one chunk of the slice. Raises on failure."""
    response = await _acompletion_over_chain(
        chain,
        messages=[
            {"role": "system", "content": ITERATIVE_SUMMARY_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Previous summary:\n{previous or '(none)'}\n\n"
                    f"New conversation slice:\n{slice_text}"
                ),
            },
        ],
        temperature=0.1,
        max_tokens=600,
    )
    text: str | None = response.choices[0].message.content
    if not text or not text.strip():
        raise RuntimeError("summary model returned no content")
    return text.strip()


def _merge_retained_facts(
    new_facts: list[CompactionFact], retained_msgs: list[dict[str, Any]]
) -> list[CompactionFact]:
    """New facts plus those a previous compaction retained, deduplicated by text."""
    facts = list(new_facts)
    for rm in retained_msgs:
        content = rm.get("content", "")
        if not isinstance(content, str):
            continue
        for line in content.split("\n"):
            line = line.strip()
            if not (line.startswith("- [") and "] (p" in line):
                continue
            try:
                cat = line.split("[", 1)[1].split("]", 1)[0]
                pri = int(line.split("(p", 1)[1].split(")", 1)[0])
                txt = line.split(") ", 1)[1] if ") " in line else ""
            except (IndexError, ValueError):
                continue
            if txt:
                facts.append(CompactionFact(category=cat, text=txt, priority=pri))
    seen: set[str] = set()
    out: list[CompactionFact] = []
    for fact in facts:
        if fact.text not in seen:
            seen.add(fact.text)
            out.append(fact)
    return out


#: Stub exchanges folded into the ledger at a time while the context is still
#: under the trigger (a fold rewrites the ledger, and the cache after it).
FOLD_BATCH = 8

#: A summary shorter than this, standing in for at least
#: ``DEGENERATE_SOURCE_CHARS`` of conversation, is a failed summary. A real one
#: is up to 300 words; a summarizer that extracts nothing answers a phrase.
DEGENERATE_SUMMARY_CHARS = 100
DEGENERATE_SOURCE_CHARS = 4_000


def _degenerate_summary(summary: str, replaced: list[dict[str, Any]]) -> bool:
    source = sum(len(_content_text(m.get("content"))) for m in replaced)
    return source >= DEGENERATE_SOURCE_CHARS and len(summary.strip()) < DEGENERATE_SUMMARY_CHARS


def _set_aside_stub_exchanges(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(messages to summarize, exchanges kept as they are).

    An exchange whose every result is already an elision stub is kept whole:
    it is small, it is the run's own evidence, and a summary model that
    extracts nothing would otherwise take the excerpt with it. Measured in a
    lab run, that is how the one result holding the fact the answer needed
    disappeared. Whole exchanges only, so every call keeps its result.
    """
    from robothor.engine.compaction_ledger import is_ledger

    summarize: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if not calls:
            (kept if is_ledger(m) else summarize).append(m)
            i += 1
            continue
        ids = {str(c.get("id")) for c in calls if isinstance(c, dict)}
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            j += 1
        results = messages[i + 1 : j]
        whole = ids == {str(r.get("tool_call_id")) for r in results}
        stubs = all(str(r.get("content") or "").startswith(STUB_PREFIX) for r in results)
        (kept if whole and results and stubs else summarize).extend(messages[i:j])
        i = j
    return summarize, kept


async def compact(
    messages: list[dict[str, Any]],
    models: list[str] | None = None,
    threshold: int = 80_000,
    drain_to: int = 60_000,
    broken_models: set[str] | None = None,
    preferred_model: str | None = None,
) -> CompactionResult:
    """Pre-pass, then one iterative summary on the agent's chain; freeze on failure.

    Retained context messages (facts) are never dropped. With no chain from the
    caller the fleet chain summarises; when every model fails, the messages are
    kept and the result is marked ``degraded``.
    """
    from robothor.engine.context import KEEP_RECENT, estimate_context_tokens
    from robothor.engine.context_fit import next_reachable_model

    # The model the trigger sized against, so an exact tokenizer, when it is
    # switched on, counts here exactly as it counted there.
    sizing = next_reachable_model(list(models or []), broken_models) or None

    # Every count runs in a worker thread: with the exact tokenizer on, each one
    # is a synchronous, CPU-bound ``litellm.token_counter`` call, and this pass
    # counts many times. On the loop that stalls every other session.
    async def measure(msgs: list[dict[str, Any]]) -> int:
        return await asyncio.to_thread(estimate_context_tokens, msgs, sizing)

    tokens_before = await measure(messages)
    if tokens_before < threshold:
        return CompactionResult(
            messages=messages, tokens_before=tokens_before, tokens_after=tokens_before
        )

    recent_budget = max(1024, drain_to // 3)
    head_len = protected_prefix_len(messages)
    protect = await asyncio.to_thread(
        protected_tail_len, messages[head_len:], recent_budget, KEEP_RECENT, sizing
    )
    working, counts = await asyncio.to_thread(
        llm_free_pre_pass, messages, protect, target=drain_to, model=sizing
    )
    est = await measure(working)
    if est >= drain_to:
        # Targeted elision can stop at, or a token over, the target: every
        # remaining old result becomes a stub before anything is summarized,
        # or a summary that extracts nothing takes it whole.
        working, more = await asyncio.to_thread(
            llm_free_pre_pass, working, protect, target=drain_to, model=sizing, elide_all=True
        )
        counts["elided"] += more["elided"]
        est = await measure(working)
    logger.info(
        "Compaction pre-pass: ~%d → ~%d tokens (%s)", tokens_before, est, sanitize_log(counts)
    )

    async def result(msgs: list[dict[str, Any]], passes: int, **extra: Any) -> CompactionResult:
        return CompactionResult(
            messages=msgs,
            passes_used=passes,
            tokens_before=tokens_before,
            tokens_after=await measure(msgs),
            **extra,
        )

    async def fold(msgs: list[dict[str, Any]], head: int, tail: int) -> list[dict[str, Any]]:
        """Stubs that alone do not fit are folded into the ledger, never dropped."""
        from robothor.engine.compaction_ledger import fold_stub_exchanges

        if await measure(msgs) <= drain_to:
            return msgs
        # Folding is triggered by the drain target, but excerpts are cut only
        # while the result is still at the trigger: that is the bound, and the
        # excerpt is what a summarizer that extracts nothing would lose.
        # Below the trigger, fold only a batch at a time: each fold rewrites
        # the ledger near the head, and everything after it leaves the cache.
        batch = 1 if await measure(msgs) >= threshold else FOLD_BATCH
        folded, n = await asyncio.to_thread(
            fold_stub_exchanges,
            msgs,
            head,
            tail,
            max(1, threshold - 1),
            sizing,
            min_exchanges=batch,
        )
        if n:
            logger.info("Compaction: folded %d elided exchange(s) into the ledger", n)
        return folded

    if est <= drain_to:
        return await result(working, 1)

    if len(working) <= KEEP_RECENT + 1:
        shrunk, n = await asyncio.to_thread(_shrink_largest, working, drain_to, model=sizing)
        folded = await fold(shrunk, head_len, protect)
        if not n and folded is shrunk and est >= tokens_before:
            # Nothing reclaimed: a no-op must not read as a pass that worked.
            return CompactionResult(
                messages=messages, tokens_before=tokens_before, tokens_after=tokens_before
            )
        return await result(folded, 1)

    head_msgs, retained_msgs, rest = _split_for_summary(working)
    previous, rest = _take_previous_summary(rest)
    split_idx = (
        await asyncio.to_thread(_recent_split, rest, recent_budget, KEEP_RECENT, sizing)
        if len(rest) > KEEP_RECENT
        else 0
    )
    old_messages, recent_messages = rest[:split_idx], rest[split_idx:]
    old_messages, kept_stubs = _set_aside_stub_exchanges(old_messages)
    if not old_messages:
        return await result(await fold(working, head_len, protect), 1)

    chain = await _summary_chain(models, broken_models, preferred_model)
    try:
        if not chain:
            raise RuntimeError("no model chain to summarise with")
        # Folded chunk by chunk, the summary carried forward, so a slice over
        # the per-call cap is summarised in full rather than from its newest end.
        summary = previous
        for chunk in _slice_chunks(old_messages):
            summary = await update_summary(summary, chunk, chain)
        if not summary:
            raise RuntimeError("nothing in the slice to summarise")
    except Exception as e:  # noqa: BLE001 — freeze, never drop the middle
        logger.warning(
            "Compaction: summary failed on every model (%s) — keeping %d messages, "
            "run marked degraded",
            sanitize_log(str(e)[:200]),
            len(working),
        )
        return await result(working, 1, degraded=True)

    if _degenerate_summary(summary, old_messages):
        # A few characters standing in for thousands is a failed summary, not a
        # short one. Keep the originals, shrunk; never discard them for it.
        logger.warning(
            "Compaction: summary of %d characters for %d messages is degenerate — "
            "shrinking the originals instead, run marked degraded",
            len(summary.strip()),
            len(old_messages),
        )
        shrunk, _ = await asyncio.to_thread(_shrink_largest, working, drain_to, model=sizing)
        return await result(await fold(shrunk, head_len, protect), 1, degraded=True)

    # Extraction reads at most FACT_INPUT_MESSAGES messages per call
    # (`build_extraction_input`), so it walks the slice the same way.
    new_facts: list[CompactionFact] = []
    for i in range(0, len(old_messages), FACT_INPUT_MESSAGES):
        new_facts.extend(
            await extract_facts(old_messages[i : i + FACT_INPUT_MESSAGES], model=chain)
        )
    facts = _merge_retained_facts(new_facts, retained_msgs)

    result_msgs = list(head_msgs)
    if facts:
        result_msgs.append(_build_retained_context_message(facts, messages))
    result_msgs.append({"role": "user", "content": f"{SUMMARY_MARKER}\n{summary}"})
    result_msgs.append(
        {
            "role": "assistant",
            "content": "Understood. I have context from our previous conversation.",
        }
    )
    result_msgs.extend(kept_stubs)
    result_msgs.extend(recent_messages)
    if await measure(result_msgs) > drain_to:
        result_msgs, _ = await asyncio.to_thread(
            _shrink_largest, result_msgs, drain_to, model=sizing
        )
        result_msgs = await fold(result_msgs, len(head_msgs), len(recent_messages))
    done = await result(result_msgs, 3, facts_extracted=facts)
    logger.info(
        "Compaction: summary updated on %s, ~%d → ~%d tokens",
        sanitize_log(chain[0]),
        tokens_before,
        done.tokens_after,
    )
    return done

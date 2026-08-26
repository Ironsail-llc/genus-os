"""LLM call layer for the Agent Engine.

Two coexisting surfaces, kept separate on purpose:

1. :class:`LLMClient` — the canonical **main agent-loop** path. Model-fallback
   chain, streaming with per-chunk timeouts + structured events, stall-watchdog
   touches, Anthropic prompt-cache kwargs, cost accounting, and message hygiene.
   ``AgentRunner`` owns one instance and the tool loop delegates to it.

2. Module-level helpers below — lightweight **auxiliary** one-shot calls for
   non-loop work:

   - :func:`llm_call` — single-model call with optional retry. (Used by
     ``buddy_critic``.)

   This is intentionally simpler than ``LLMClient`` (no watchdog/cost/cache
   wiring) and serves callers that just need a quick completion.

Convergence note (Phase A / Slice 4): an audit found the originally-anticipated
"migrate planner/verifier/compaction/PDF onto a shared client" never happened —
in practice only ``buddy_critic`` calls :func:`llm_call`. The unused
``llm_call_with_fallback`` / ``llm_call_streaming`` helpers were removed
2026-07-13 after a year-class soak with no production callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
import time as _time
from collections.abc import Awaitable, Callable  # noqa: TC003
from typing import TYPE_CHECKING, Any

import litellm

from robothor.engine.codex_provider import CodexProviderError, is_codex_model
from robothor.engine.codex_provider import acompletion as codex_acompletion
from robothor.engine.metrics import LLM_CALL_DURATION, LLM_CALLS_TOTAL, LLM_TOKENS_TOTAL
from robothor.engine.model_breaker import _current_run_id_var, get_model_breaker
from robothor.engine.retry import retry_async
from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.stall_watchdog import _active_watchdog_var

if TYPE_CHECKING:
    from robothor.engine.session import AgentSession
    from robothor.engine.stall_watchdog import _StallWatchdog

logger = logging.getLogger(__name__)

# ── LLM request timeouts (shared with runner, which re-exports these) ──
# Max seconds to wait for the next streaming chunk before aborting and
# falling back to the next model. Prevents stalled streams from hanging
# the entire run (the stream *creation* timeout is separate — see
# ``LLMClient._build_llm_kwargs``'s ``timeout``).
STREAM_CHUNK_TIMEOUT = 90

# HTTP-level timeout passed to litellm.acompletion for the initial
# request (stream creation for streaming, full response for
# non-streaming). Must be longer than STREAM_CHUNK_TIMEOUT so a slow
# first chunk is handled by the per-chunk watchdog rather than killing
# the whole request. Ollama gets more headroom for cold-start loads.


def _timeout_from_env(name: str, default: int) -> int:
    """Read a positive-integer timeout from the environment, or the default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using default %ds", name, raw, default)
        return default
    return value if value > 0 else default


LLM_REQUEST_TIMEOUT = _timeout_from_env("ROBOTHOR_LLM_TIMEOUT", 120)
# Non-interactive (cron/workflow) runs get more headroom: nobody is waiting
# on the reply, and a large classify context on a reasoning model can
# legitimately take >120s of wall-clock generation (2026-08-20: all three
# chain models were cancelled at exactly 120s while small concurrent calls
# succeeded — the chain exhausted on slowness, not provider death).
LLM_REQUEST_TIMEOUT_BATCH = _timeout_from_env("ROBOTHOR_LLM_TIMEOUT_BATCH", 300)
LLM_REQUEST_TIMEOUT_OLLAMA = 600

# Trigger types whose runs are batch-shaped (no human waiting on the reply)
# and therefore get LLM_REQUEST_TIMEOUT_BATCH per model.
_BATCH_TRIGGER_TYPES = frozenset({"cron", "workflow"})

# Marker prepended to engine-injected context when it is rewritten from the
# ``developer`` role to a user turn (Anthropic-family models only — see
# ``LLMClient._normalize_developer_role``). Keeps the provenance visible to the
# model so it does not read engine context as operator speech.
ENGINE_CONTEXT_PREFIX = "[engine] "

# One in-place retry per model for transient failures (timeout / 5xx) before
# advancing the fallback chain, with a short jitter so a provider blip is not
# re-hit instantly. A transient 502 used to burn the model's only attempt and
# exhaust the whole chain within minutes.
TRANSIENT_RETRIES_PER_MODEL = 1
TRANSIENT_RETRY_JITTER_MIN = 2.0
TRANSIENT_RETRY_JITTER_MAX = 5.0
_TRANSIENT_RETRY_STATUSES = frozenset({500, 502, 503, 504})


class EmptyCompletionError(RuntimeError):
    """The model returned a 200 with neither text nor a tool call.

    Measured 2026-08-22: two agent-architect benchmark runs completed in under
    20s with one llm_call, zero tool calls and output_text of length 0, while
    four sibling cases in the same run produced 175-1386 characters. An empty
    completion arrives as a successful response, so it never raised and was
    never retried -- the run was recorded as a success that produced no answer.
    """


def _is_empty_completion(result: Any) -> bool:
    """True when a response carries neither text nor a tool call.

    A response with no text but WITH tool calls is the normal shape of every
    tool-using turn and must never be treated as empty -- retrying those would
    re-issue side-effectful calls. Anything we cannot parse is reported
    non-empty, so an unfamiliar response shape can never drive a retry loop.
    """
    try:
        message = result.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return False
    if getattr(message, "tool_calls", None):
        return False
    content = getattr(message, "content", None)
    if content is None:
        return True
    return isinstance(content, str) and not content.strip()


def _is_transient_model_error(e: BaseException) -> bool:
    """True for failures worth one same-model retry: timeouts, 5xx, empties."""
    if isinstance(e, TimeoutError | EmptyCompletionError):
        return True
    return getattr(e, "status_code", None) in _TRANSIENT_RETRY_STATUSES


def _safe_token_count(usage: Any, attr: str) -> int:
    """Extract a token count from a response usage object, returning 0 on failure."""
    try:
        val = getattr(usage, attr, 0)
        return int(val) if val else 0
    except (TypeError, ValueError):
        return 0


# Exceptions worth retrying — transient network / provider errors.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.Timeout,
)


async def llm_call(
    messages: list[dict[str, Any]],
    *,
    model: str,
    temperature: float = 0.3,
    json_mode: bool = False,
    timeout: int | float = 120,
    max_retries: int = 1,
    max_tokens: int | None = None,
) -> Any:
    """Single-model LLM call with timeout and optional retry.

    Args:
        messages: Chat messages in OpenAI format.
        model: Model identifier (litellm format).
        temperature: Sampling temperature.
        json_mode: If True, request ``response_format={"type": "json_object"}``.
        timeout: Per-attempt timeout in seconds.
        max_retries: Total attempts (1 = no retry, 2 = one retry, etc.).
        max_tokens: Optional max output tokens.

    Returns:
        The ``litellm.ModelResponse`` object.

    Raises:
        The last exception if all attempts are exhausted.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    async def _attempt() -> Any:
        t0 = _time.monotonic()
        try:
            call = codex_acompletion if is_codex_model(model) else litellm.acompletion
            resp = await asyncio.wait_for(call(**kwargs), timeout=timeout)
            LLM_CALLS_TOTAL.labels(model=model, status="success").inc()
            LLM_CALL_DURATION.labels(model=model).observe(_time.monotonic() - t0)
            usage = getattr(resp, "usage", None)
            if usage:
                LLM_TOKENS_TOTAL.labels(model=model, direction="input").inc(
                    _safe_token_count(usage, "prompt_tokens")
                )
                LLM_TOKENS_TOTAL.labels(model=model, direction="output").inc(
                    _safe_token_count(usage, "completion_tokens")
                )
            return resp
        except Exception:
            LLM_CALLS_TOTAL.labels(model=model, status="error").inc()
            LLM_CALL_DURATION.labels(model=model).observe(_time.monotonic() - t0)
            raise

    return await retry_async(
        _attempt,
        max_attempts=max_retries,
        retryable_exceptions=_RETRYABLE_EXCEPTIONS,
        backoff_base=1.0,
    )


#: What the agent is told in place of a picture its model cannot see. Plain
#: text, in the tool result where the image would have been, so the agent
#: learns the capability is unavailable *for this model* and can fall back to
#: inspecting the file programmatically.
IMAGE_UNSUPPORTED_NOTE = (
    "[the image could not be shown to this model — it accepts text only. "
    "Inspect the file programmatically instead, e.g. with Pillow via exec.]"
)

#: Provider phrasings for "I cannot accept an image". OpenRouter answers a
#: text-only model with a 404 whose message is the giveaway; others use a
#: 400 with prose. Matched on the message, not the status, because a bare
#: 404 also means "no such model".
_IMAGE_UNSUPPORTED_MARKERS = (
    "support image input",
    "image_url is not supported",
    "does not support image",
    "image input is not supported",
    "unsupported content type: image",
)


def is_image_unsupported_error(e: Exception) -> bool:
    """Did the provider refuse specifically because of an image?"""
    msg = str(e).lower()
    return any(marker in msg for marker in _IMAGE_UNSUPPORTED_MARKERS)


def strip_image_blocks(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Return a copy of `messages` with image blocks replaced by a note.

    The caller's list is never mutated: a retry that rewrote history in place
    would leave the conversation permanently blind even after a vision-capable
    fallback took over. Text blocks alongside the image (the caption naming
    its dimensions) survive — that is real context about what the agent
    looked at.
    """
    changed = False
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        if not any(
            isinstance(b, dict) and b.get("type") in ("image_url", "image") for b in content
        ):
            out.append(msg)
            continue
        changed = True
        texts = [
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        note = IMAGE_UNSUPPORTED_NOTE
        if texts:
            note = " ".join(texts) + " " + note
        out.append({**msg, "content": note})
    return out, changed


class LLMClient:
    """LLM dispatch, model-fallback, streaming, cost, and message hygiene.

    Extracted from ``AgentRunner`` (Phase A / Slice 1) with no behavior change.
    Stateless w.r.t. a single run: per-run state (``broken_models``, messages,
    session) flows in as arguments, so one instance can be shared across runs
    like the tool registry is.

    The watchdog is read from the ``_active_watchdog_var`` ContextVar — the same
    source ``AgentRunner._active_watchdog`` reads — so there is no back-reference
    to the runner and no import cycle.
    """

    @property
    def _active_watchdog(self) -> _StallWatchdog | None:
        """Read-only view over the per-task stall watchdog ContextVar."""
        return _active_watchdog_var.get()

    # ─── Cost ────────────────────────────────────────────────────────

    def _response_cost(
        self,
        *,
        response: Any,
        model_used: str,
        models: list[str],
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
    ) -> float:
        """Compute the USD cost for one LLM response.

        codex/* models are subscription-billed; litellm cannot price them
        (it doesn't recognize the provider prefix) and raises on every call,
        spamming the log. Those are priced from the registry ($0) with the
        litellm call skipped entirely. Every other model keeps the original
        litellm-first, registry-fallback behavior — including the fallback
        case where codex failed over to an OpenRouter model.

        G2a: pricing is keyed to ``model_used`` (the model that actually
        answered), not ``models[0]`` (the configured primary). On a fallback
        run — the production default while the codex primary was unreachable
        (audit 2026-05-29) — keying to models[0] reported the *primary's* price
        for a response the *fallback* produced. ``models`` is retained in the
        signature for call-site compatibility but is no longer read.
        """
        del models  # G2a: cost is keyed to model_used, not the configured primary
        if is_codex_model(model_used):
            return self._calculate_cost(
                model_used,
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
            )
        try:
            cost = litellm.completion_cost(completion_response=response, model=model_used)
            if cost and cost > 0:
                return cost
        except Exception as e:
            logger.warning("litellm cost calculation failed, using fallback: %s", e)
        return self._calculate_cost(
            model_used,
            input_tokens,
            output_tokens,
            cache_creation_tokens,
            cache_read_tokens,
        )

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """Calculate cost from litellm model registry, with cache-aware pricing."""
        info = litellm.model_cost.get(model, {})
        input_rate: float = info.get("input_cost_per_token", 0.0)
        output_rate: float = info.get("output_cost_per_token", 0.0)

        # Use our registry for cache pricing (litellm doesn't expose these yet)
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits(model)
        cache_write_rate = limits.cache_write_cost_per_token or input_rate
        cache_read_rate = limits.cache_read_cost_per_token or (input_rate * 0.1)

        # Coerce to int to guard against MagicMock or None from test mocks
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        cache_creation_tokens = int(cache_creation_tokens or 0)
        cache_read_tokens = int(cache_read_tokens or 0)

        # Non-cached input = total input minus tokens that were cache hits
        regular_input = max(0, input_tokens - cache_read_tokens)
        return (
            regular_input * input_rate
            + output_tokens * output_rate
            + cache_creation_tokens * cache_write_rate
            + cache_read_tokens * cache_read_rate
        )

    # ─── Dispatch ────────────────────────────────────────────────────

    async def _do_llm_call(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None,
        broken_models: set[str],
        temperature: float,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Any:
        """Dispatch to streaming or non-streaming LLM call."""
        # Batch-shaped runs (cron/workflow) get the higher non-interactive
        # per-model timeout; interactive triggers keep the default.
        timeout_override: float | None = None
        trigger = getattr(session.run, "trigger_type", None)
        if trigger is not None and str(trigger) in _BATCH_TRIGGER_TYPES:
            timeout_override = float(LLM_REQUEST_TIMEOUT_BATCH)

        # Expose the run id to the model breaker so a trip during this call
        # can be recorded as a guardrail event against the run.
        run_token = _current_run_id_var.set(getattr(session.run, "id", None))
        try:
            if on_content or on_stream_event:
                return await self._call_llm_streaming(
                    session.messages,
                    models,
                    tool_schemas,
                    on_content,
                    broken_models=broken_models,
                    temperature=temperature,
                    on_stream_event=on_stream_event,
                    timeout_override=timeout_override,
                )
            return await self._call_llm(
                session.messages,
                models,
                tool_schemas,
                broken_models=broken_models,
                temperature=temperature,
                timeout_override=timeout_override,
            )
        finally:
            _current_run_id_var.reset(run_token)

    # ─── Pre-flight ──────────────────────────────────────────────────

    @staticmethod
    def sizing_model(models: list[str], broken_models: set[str] | None = None) -> str:
        """Return the model the context-window math should be sized against.

        G2b: this is the first model in ``models`` not already marked broken —
        i.e. the one the fallback loop will actually try next — not ``models[0]``
        (the configured primary). When the primary is down and the run is on a
        smaller-window fallback, sizing against the primary's window (e.g. 1M)
        can overflow the fallback (e.g. 200K). Falls back to ``models[0]`` when
        every model is broken, or ``""`` for an empty list.
        """
        broken = broken_models or set()
        for model in models:
            if model not in broken:
                return model
        return models[0] if models else ""

    async def _prepare_llm_call(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        broken_models: set[str] | None = None,
    ) -> int:
        """Shared pre-flight: compress context and estimate input tokens.

        Mutates messages in-place. Returns estimated input token count.
        """
        from robothor.engine.context import estimate_tokens, maybe_compress
        from robothor.engine.model_registry import get_model_limits

        try:
            from robothor.engine.run_budget import proactive_compaction_threshold

            model_limits = get_model_limits(self.sizing_model(models, broken_models))
            # Same clamped threshold as the in-loop trigger. The old
            # 0.75-of-window guard was 786K tokens on the fleet primary's 1M
            # window — unreachable, zero firings in 7 days. When the in-loop
            # pass just compacted, this no-ops (estimate is under threshold);
            # it exists for the paths that call the client without the loop.
            compress_threshold = proactive_compaction_threshold(model_limits.max_input_tokens)
            messages[:] = await maybe_compress(messages, models, threshold=compress_threshold)
        except Exception as e:
            logger.warning("Pre-flight compression failed: %s", _sanitize(e))

        return estimate_tokens(messages)

    # ─── Message hygiene ─────────────────────────────────────────────

    @staticmethod
    def _validate_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop orphaned tool messages whose tool_call_id has no matching tool_use.

        This is a defense-in-depth measure: if compaction, checkpoint restore,
        or format conversion ever corrupts the message history, this prevents
        cryptic "unexpected tool_use_id in tool_result blocks" API errors.
        """
        valid_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        valid_ids.add(tc_id)

        if not valid_ids:
            return messages  # no tool calls at all — nothing to validate

        cleaned: list[dict[str, Any]] = []
        dropped = 0
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id") not in valid_ids:
                dropped += 1
                continue
            cleaned.append(msg)

        if dropped:
            logger.warning(
                "Dropped %d orphaned tool_result message(s) — "
                "no matching tool_use in assistant messages",
                dropped,
            )

        return cleaned if dropped else messages

    @staticmethod
    def _is_anthropic_family(model: str) -> bool:
        """True for models served by the Anthropic Messages API shape.

        Matches the model *string* rather than a single prefix so every routing
        spelling is covered: ``anthropic/claude-…`` (direct),
        ``openrouter/anthropic/claude-…`` (proxied), ``bedrock/anthropic.claude-…``
        and bare ``claude-…`` aliases.
        """
        lowered = model.lower()
        return "anthropic/" in lowered or "claude" in lowered

    @staticmethod
    def _normalize_developer_role(
        model: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Rewrite engine ``developer`` turns as user turns for Anthropic models.

        The engine injects context (plan, budget warnings, verification-retry
        feedback) with ``role=ENGINE_CONTEXT_ROLE`` — ``"developer"`` — often at
        the conversation TAIL. litellm maps ``developer`` → ``system`` for
        non-OpenAI providers, and the Anthropic transformation then *hoists*
        every system turn out of the message list into the top-level ``system``
        parameter. What Anthropic actually receives therefore ends with the
        assistant turn that preceded the developer message, and it rejects that:

            "This model does not support assistant message prefill.
             The conversation must end with a user message."

        ``_guard_trailing_assistant`` cannot see this — at guard time the list
        still ends with the developer turn. Since MiMo (and other OpenAI-shaped
        providers) accept ``developer`` verbatim, the failure hit only the
        Anthropic last-resort leg of the fallback chain, i.e. exactly when it
        was needed most. Normalizing here, *before* the guard, keeps the context
        in the conversation and leaves a legitimate user turn at the tail.

        Non-anthropic models are returned untouched (same list object), so their
        payloads stay byte-identical.
        """
        from robothor.engine.session import ENGINE_CONTEXT_ROLE

        if not LLMClient._is_anthropic_family(model):
            return messages
        if not any(m.get("role") == ENGINE_CONTEXT_ROLE for m in messages):
            return messages

        normalized: list[dict[str, Any]] = []
        converted = 0
        for msg in messages:
            if msg.get("role") != ENGINE_CONTEXT_ROLE:
                normalized.append(msg)
                continue
            converted += 1
            rewritten = dict(msg)
            rewritten["role"] = "user"
            content = msg.get("content")
            if isinstance(content, list):
                rewritten["content"] = [
                    {"type": "text", "text": ENGINE_CONTEXT_PREFIX.strip()},
                    *content,
                ]
            else:
                rewritten["content"] = f"{ENGINE_CONTEXT_PREFIX}{content or ''}"
            normalized.append(rewritten)

        logger.debug(
            "Normalized %d developer-role message(s) to user turns for %s",
            converted,
            model,
        )
        return normalized

    @staticmethod
    def _guard_trailing_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop trailing assistant messages before an LLM call.

        OpenRouter-proxied Anthropic (via Azure/Google) rejects requests whose
        final message is an assistant turn with:
            "This model does not support assistant message prefill.
             The conversation must end with a user message."

        A trailing assistant normally indicates an orphaned turn — e.g. a prior
        run's response that was carried into history without its follow-up, or
        a checkpoint restored after a user turn was lost. Drop it so the LLM
        call can proceed.

        Strips *every* trailing assistant turn, not just the last one: there is
        only one pre-flight pass per call, so leaving a second orphan behind
        would still trip the prefill rejection.
        """
        end = len(messages)
        while end > 0 and messages[end - 1].get("role") == "assistant":
            end -= 1
        if end == len(messages):
            return messages
        logger.warning(
            "Dropping %d trailing assistant message(s) before LLM call (prefill-rejection guard)",
            len(messages) - end,
        )
        return messages[:end]

    # ─── Request kwargs ──────────────────────────────────────────────

    @staticmethod
    def _build_llm_kwargs(
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        input_est: int,
        temperature: float,
        *,
        stream: bool = False,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Build kwargs dict for litellm.acompletion.

        ``request_timeout`` overrides the HTTP-level timeout so it stays in
        step with the caller's per-call ``asyncio.timeout`` budget (e.g. the
        batch timeout for cron/workflow runs).
        """
        from robothor.engine.model_registry import (
            get_model_limits,
            get_output_tokens,
            supports_cache_control,
        )

        limits = get_model_limits(model)
        actual_model = model

        # For models that support Anthropic-style prompt caching, enable it on
        # the system message by converting it to content-block format with
        # cache_control. This is now a catalog-driven capability lookup (see
        # model_registry's supports_cache_control helper) instead of a bare
        # provider-prefix string check, so it follows fleet model changes
        # automatically. See that helper's docstring for why OpenRouter
        # models (including "openrouter/anthropic/...") are excluded —
        # litellm sends them via the OpenAI-compatible path and the mixed
        # content-block format causes tool_use/tool_result pairing failures.
        if supports_cache_control(model) and messages and messages[0].get("role") == "system":
            messages = list(messages)  # shallow copy to avoid mutating original
            sys_content = messages[0].get("content")
            if isinstance(sys_content, str):
                sys_msg = dict(messages[0])
                # Split into static (cacheable) + dynamic (time context) blocks.
                # The dynamic tail starts at the last "---" separator before "Current time:".
                split_marker = "\n\n---\n\nCurrent time:"
                split_idx = sys_content.rfind(split_marker)
                if split_idx > 0:
                    static_part = sys_content[:split_idx]
                    dynamic_part = sys_content[split_idx + len("\n\n---\n\n") :]
                    sys_msg["content"] = [
                        {
                            "type": "text",
                            "text": static_part,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {
                            "type": "text",
                            "text": dynamic_part,
                        },
                    ]
                else:
                    # Fallback: cache the whole thing (no time context found)
                    sys_msg["content"] = [
                        {
                            "type": "text",
                            "text": sys_content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                messages[0] = sys_msg

        # Defense in depth: drop orphaned tool_result messages that would
        # cause "unexpected tool_use_id" API errors.
        messages = LLMClient._validate_tool_pairs(messages)

        # Anthropic hoists developer/system turns out of the message list, which
        # can strand an assistant turn at the tail. Rewrite them as user turns
        # BEFORE the prefill guard runs so the guard sees the real terminator.
        # No-op (same list object) for every non-anthropic model.
        messages = LLMClient._normalize_developer_role(model, messages)

        # Defense in depth: drop a trailing assistant message, which OpenRouter-
        # proxied Anthropic rejects with "model does not support prefill".
        messages = LLMClient._guard_trailing_assistant(messages)

        kwargs: dict[str, Any] = {
            "model": actual_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": get_output_tokens(model, input_est),
            "timeout": (
                request_timeout
                if request_timeout is not None
                else (
                    LLM_REQUEST_TIMEOUT_OLLAMA
                    if model.startswith("ollama_chat/")
                    else LLM_REQUEST_TIMEOUT
                )
            ),
        }
        # Pin OpenRouter routing for Anthropic models to the Anthropic-direct
        # backend. OpenRouter's default load-balancing also fans out to Google
        # Vertex and Amazon Bedrock, both of which reject assistant-prefill
        # ("This model does not support assistant message prefill") and
        # ephemeral-cache_control content blocks. Anthropic-direct supports
        # both. allow_fallbacks=False means an Anthropic outage falls through
        # to our existing model_fallbacks chain (MiMo, DeepSeek, etc.) rather
        # than silently routing to a less-compatible backend.
        if model.startswith("openrouter/anthropic/"):
            kwargs["extra_body"] = {
                "provider": {
                    "order": ["Anthropic"],
                    "allow_fallbacks": False,
                }
            }
        if stream:
            kwargs["stream"] = True
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if limits.supports_thinking:
            from robothor.engine.model_registry import THINKING_BUDGET_TOKENS

            kwargs["temperature"] = 1.0  # Required by Anthropic API
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": THINKING_BUDGET_TOKENS,
            }
        return kwargs

    # ─── Model error handling ────────────────────────────────────────

    @staticmethod
    def _handle_model_error(
        e: Exception,
        model: str,
        broken_models: set[str] | None,
        *,
        streaming: bool = False,
    ) -> None:
        """Handle model failure: mark broken or log warning."""
        status = getattr(e, "status_code", None)
        is_timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError))
        # Provider-availability failures (e.g. the Codex CLI missing from the
        # engine's PATH, or a misconfigured local provider) carry no HTTP
        # status, so they previously slipped into the generic warning branch and
        # the primary was never marked broken nor flagged — that is exactly how
        # codex/gpt-5.5 silently fell back to mimo for the whole fleet without a
        # single ERROR line (audit 2026-05-29). Treat them like other hard
        # provider failures so the PRIMARY-failed path fires.
        # CodexProviderError is precise. A bare FileNotFoundError only means
        # "provider binary missing" for the codex CLI path — for any other model
        # it could be an unrelated transient (a cred/CA/config file), so don't
        # let it falsely mark a non-codex model broken.
        is_provider_down = isinstance(e, CodexProviderError) or (
            isinstance(e, FileNotFoundError) and is_codex_model(model)
        )
        # Mark broken for auth, rate limit, provider failures, and timeouts
        if broken_models is not None and (
            status in (401, 402, 403, 429, 500, 502, 503, 504) or is_timeout or is_provider_down
        ):
            # First model to fail = primary model — log at ERROR for visibility
            is_primary = len(broken_models) == 0
            broken_models.add(model)
            if is_timeout:
                reason = "timeout"
            elif is_provider_down:
                reason = f"provider unavailable: {e}"
            else:
                reason = str(status)
            if is_primary:
                logger.error(
                    "PRIMARY model %s failed (%s), falling back — primary_model_fallback=True",
                    _sanitize(model),
                    _sanitize(reason),
                )
            else:
                logger.warning(
                    "Model %s failed (%s), removing from rotation for this run",
                    _sanitize(model),
                    _sanitize(reason),
                )
        else:
            suffix = " (streaming)" if streaming else ""
            logger.warning("Model %s%s failed: %s", _sanitize(model), suffix, _sanitize(e))

    async def _call_with_image_fallback(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> Any:
        """One litellm call, surviving a model that cannot accept images.

        A text-only model answers an image content block with a hard refusal,
        and before this that refusal walked the whole fallback chain and ended
        the run — an agent lost everything because it looked at a picture.
        Here the images are stripped, the agent is told plainly that this
        model cannot see and to inspect the file programmatically, and the
        SAME model is retried once. Every other failure is re-raised untouched
        so the normal fallback and breaker logic still owns it.
        """
        try:
            return await litellm.acompletion(**kwargs)
        except Exception as e:
            if not is_image_unsupported_error(e):
                raise
            stripped, changed = strip_image_blocks(messages)
            if not changed:
                raise
            logger.warning(
                "Model %s cannot accept images — retrying without them; "
                "the agent is told to inspect the file programmatically",
                _sanitize(model),
            )
            return await litellm.acompletion(**{**kwargs, "messages": stripped})

    # ─── Non-streaming call ──────────────────────────────────────────

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
        timeout_override: float | None = None,
    ) -> Any:
        """Call LLM with model fallback. Returns litellm response or None.

        Each model gets one in-place retry (short jitter) for transient
        failures — timeouts and 5xx — before the chain advances, so a single
        provider blip no longer burns a model's only attempt.
        """
        input_est = await self._prepare_llm_call(messages, models, broken_models)
        last_error: Exception | None = None

        logger.debug(
            "LLM call with models: %s (broken: %s)",
            _sanitize(models),
            _sanitize(broken_models or set()),
        )
        breaker = get_model_breaker()
        for model in models:
            if broken_models and model in broken_models:
                continue
            if breaker.is_open(model):
                # This model has failed repeatedly and is in cooldown. Skipping
                # it here is the point: otherwise a dead provider costs the full
                # per-call timeout on every run, forever (codex/* did exactly
                # that for a month).
                logger.info("skipping %s — circuit breaker open", _sanitize(model))
                continue
            # Per-call timeout (seconds) — wraps each provider call so the
            # runner cancels and falls through if the provider hangs. The
            # `timeout` kwarg already passed to litellm is best-effort and
            # was observed silently ignored, causing 1800s stalls against
            # codex/gpt-5.5 in the 2026-05-28 incident.
            if model.startswith("ollama_chat/"):
                per_call_timeout: float = LLM_REQUEST_TIMEOUT_OLLAMA
            else:
                per_call_timeout = (
                    timeout_override if timeout_override is not None else LLM_REQUEST_TIMEOUT
                )
            attempts = 1 + TRANSIENT_RETRIES_PER_MODEL
            for attempt in range(attempts):
                try:
                    kwargs = self._build_llm_kwargs(
                        model,
                        messages,
                        tools,
                        input_est,
                        temperature,
                        request_timeout=per_call_timeout,
                    )
                    async with asyncio.timeout(per_call_timeout):
                        if is_codex_model(model):
                            result = await codex_acompletion(**kwargs)
                        else:
                            result = await self._call_with_image_fallback(
                                model=model, messages=kwargs.get("messages", []), kwargs=kwargs
                            )
                    if _is_empty_completion(result):
                        # Not a finished answer. Raising routes this into the
                        # transient-retry path below rather than returning a
                        # run that silently produced nothing.
                        raise EmptyCompletionError(f"{model} returned no content and no tool call")
                    breaker.record_success(model)
                    return result
                except Exception as e:
                    last_error = e
                    is_timeout = isinstance(e, TimeoutError)
                    if attempt < attempts - 1 and _is_transient_model_error(e):
                        delay = random.uniform(
                            TRANSIENT_RETRY_JITTER_MIN, TRANSIENT_RETRY_JITTER_MAX
                        )
                        logger.warning(
                            "Model %s transient failure (%s) — retrying same model "
                            "in %.1fs (attempt %d/%d)",
                            _sanitize(model),
                            _sanitize(f"timeout after {per_call_timeout}s" if is_timeout else e),
                            delay,
                            attempt + 1,
                            attempts,
                        )
                        if self._active_watchdog:
                            self._active_watchdog.touch(f"model_retry:{model}")
                        await asyncio.sleep(delay)
                        continue
                    # Giving up on this model — record the failure once per
                    # chain advancement (a retried-then-recovered blip must
                    # not count double toward the breaker threshold).
                    if is_timeout:
                        breaker.record_failure(model, reason=f"timeout after {per_call_timeout}s")
                        logger.warning(
                            "LLM call to %s exceeded %ds — cancelling and falling back",
                            _sanitize(model),
                            per_call_timeout,
                        )
                    else:
                        breaker.record_failure(model, reason=str(e)[:120])
                    self._handle_model_error(e, model, broken_models)
                    if self._active_watchdog:
                        self._active_watchdog.touch(f"model_fallback:{model}")
                    break  # advance to the next model in the chain

        # repr(), not str(): str(TimeoutError()) is "" and produced the
        # infamous blank "last error: " exhaustion log.
        logger.error(
            "All models failed. Models: %s, broken: %s, last error: %s",
            _sanitize(models),
            _sanitize(broken_models or set()),
            _sanitize(repr(last_error)),
        )
        return None

    # ─── Streaming call ──────────────────────────────────────────────

    async def _call_llm_streaming(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None = None,
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        timeout_override: float | None = None,
    ) -> Any:
        """Call LLM with streaming. Returns reconstructed ModelResponse.

        No in-place transient retry here (unlike ``_call_llm``): a retry after
        partial content has already been emitted to ``on_content`` would show
        the user duplicated text; stalled streams already fall back via the
        per-chunk timeout.

        Emits structured events to ``on_stream_event`` if provided:
        - ``{"type": "text_delta", "delta": "...", "accumulated": "..."}``
        - ``{"type": "tool_use_start", "tool_name": "...", "call_id": "..."}``
        - ``{"type": "tool_use_delta", "delta": "...", "call_id": "..."}``
        - ``{"type": "usage", "input_tokens": N, "output_tokens": N}``
        - ``{"type": "message_stop"}``
        """
        input_est = await self._prepare_llm_call(messages, models, broken_models)
        last_error: Exception | None = None

        async def _emit(event: dict[str, Any]) -> None:
            if on_stream_event:
                with contextlib.suppress(Exception):
                    await on_stream_event(event)

        for model in models:
            if broken_models and model in broken_models:
                continue
            # Per-call timeout for the initial stream-creation await.
            # Subsequent chunk reads are guarded by STREAM_CHUNK_TIMEOUT
            # in the consumption loop below. See _call_llm for context.
            if model.startswith("ollama_chat/"):
                per_call_timeout: float = LLM_REQUEST_TIMEOUT_OLLAMA
            else:
                per_call_timeout = (
                    timeout_override if timeout_override is not None else LLM_REQUEST_TIMEOUT
                )
            try:
                kwargs = self._build_llm_kwargs(
                    model,
                    messages,
                    tools,
                    input_est,
                    temperature,
                    stream=True,
                    request_timeout=per_call_timeout,
                )
                if is_codex_model(model):
                    async with asyncio.timeout(per_call_timeout):
                        result = await codex_acompletion(**kwargs)
                    content = str(result.choices[0].message.content or "")
                    if on_content and content:
                        await on_content(content)
                    await _emit({"type": "text_delta", "delta": content, "accumulated": content})
                    await _emit(
                        {
                            "type": "usage",
                            "input_tokens": 0,
                            "output_tokens": 0,
                        }
                    )
                    await _emit({"type": "message_stop"})
                    return result

                stream_start = time.monotonic()
                async with asyncio.timeout(per_call_timeout):
                    stream = await litellm.acompletion(**kwargs)

                chunks: list[Any] = []
                accumulated_content = ""
                has_tool_calls = False
                ttft_logged = False
                seen_tool_ids: set[str] = set()

                # Consume stream with per-chunk timeout so stalled streams
                # fall back to the next model instead of hanging the run.
                chunk_iter = stream.__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            chunk_iter.__anext__(), timeout=STREAM_CHUNK_TIMEOUT
                        )
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        logger.warning(
                            "Stream stalled for %ds, aborting model=%s",
                            STREAM_CHUNK_TIMEOUT,
                            _sanitize(model),
                        )
                        raise TimeoutError(
                            f"Stream stalled after {STREAM_CHUNK_TIMEOUT}s of no chunks"
                        ) from None

                    chunks.append(chunk)

                    # Progress-based watchdog: only real content (text or
                    # tool-call bytes) counts as activity. SSE keepalive
                    # chunks and empty frames used to keep the watchdog
                    # alive on dead streams — that was the 07:00/08:00
                    # failure mode (900s of pings, 0 tokens, hard-killed).
                    if not chunk.choices:
                        # Check for usage in non-choice chunks (some providers)
                        usage = getattr(chunk, "usage", None)
                        if usage:
                            await _emit(
                                {
                                    "type": "usage",
                                    "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                                    "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                                }
                            )
                        continue
                    delta = chunk.choices[0].delta
                    if getattr(delta, "content", None):
                        if not ttft_logged:
                            ttft_ms = int((time.monotonic() - stream_start) * 1000)
                            logger.info("TTFT %dms model=%s", ttft_ms, _sanitize(model))
                            ttft_logged = True
                        accumulated_content += delta.content
                        if self._active_watchdog:
                            self._active_watchdog.touch(f"stream_text:{model}")
                        await _emit(
                            {
                                "type": "text_delta",
                                "delta": delta.content,
                                "accumulated": accumulated_content,
                            }
                        )
                        if not has_tool_calls and on_content:
                            with contextlib.suppress(Exception):
                                await on_content(accumulated_content)
                    if getattr(delta, "tool_calls", None):
                        has_tool_calls = True
                        if self._active_watchdog:
                            self._active_watchdog.touch(f"stream_tool_call:{model}")
                        for tc in delta.tool_calls:
                            tc_id = getattr(tc, "id", None)
                            tc_fn = getattr(tc, "function", None)
                            if tc_id and tc_id not in seen_tool_ids:
                                seen_tool_ids.add(tc_id)
                                await _emit(
                                    {
                                        "type": "tool_use_start",
                                        "tool_name": getattr(tc_fn, "name", "") if tc_fn else "",
                                        "call_id": tc_id,
                                    }
                                )
                            if tc_fn and getattr(tc_fn, "arguments", None):
                                await _emit(
                                    {
                                        "type": "tool_use_delta",
                                        "delta": tc_fn.arguments,
                                        "call_id": tc_id or "",
                                    }
                                )

                await _emit({"type": "message_stop"})
                # Final progress tick — we have a complete response to return.
                if self._active_watchdog:
                    self._active_watchdog.touch(f"stream_complete:{model}")
                return litellm.stream_chunk_builder(chunks)
            except TimeoutError as te:
                self._handle_model_error(
                    te,
                    model,
                    broken_models,
                    streaming=True,
                )
                last_error = te
                # Model rotation is activity — don't let watchdog kill us mid-fallback
                if self._active_watchdog:
                    self._active_watchdog.touch(f"stream_timeout_fallback:{model}")
            except Exception as e:
                self._handle_model_error(e, model, broken_models, streaming=True)
                last_error = e
                if self._active_watchdog:
                    self._active_watchdog.touch(f"stream_error_fallback:{model}")

        # repr(): str(TimeoutError()) is "" — see _call_llm's exhaustion log.
        logger.error("All models failed (streaming). Last error: %s", _sanitize(repr(last_error)))
        return None

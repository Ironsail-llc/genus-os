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
from robothor.engine.key_pool import KeyPool, Retirement, env_var_for_model, keys_from_env
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


#: Longest a run will wait out a rate limit before giving up on that model.
#: A provider can name an hour; a run that sleeps an hour inside its own
#: wall-clock ceiling has thrown the budget away in a different manner.
MAX_RATE_LIMIT_WAIT = 30.0

#: Fallback wait when a 429 carries no interval.
_DEFAULT_RATE_LIMIT_WAIT = 5.0

#: Headers providers use to say when to come back.
_RETRY_AFTER_HEADERS = (
    "retry-after",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-reset",
)

#: A spent budget, not a busy provider. No wait fixes it, and no other model
#: on the same key will work either. A campaign lost hours to this: an
#: exhausted key produced task after task of zeros indistinguishable from
#: capability failures.
_CREDIT_EXHAUSTED_MARKERS = (
    "key limit exceeded",
    "insufficient credit",
    "insufficient_quota",
    "exceeded your current quota",
    "billing hard limit",
    "payment required",
    "not enough credits",
)

#: A cap tied to a CALENDAR window rather than a balance. Still "the
#: account cannot pay", so these stay inside _CREDIT_EXHAUSTED_MARKERS and
#: the chain still stops dialling — but topping up does not clear them, so
#: the key must not be retried on the short spend-cap cooldown. Retrying a
#: weekly cap every 900s revives it ~96 times a day and turns one dead
#: credential into an all-day error storm across every agent.
_PERIODIC_QUOTA_MARKERS = (
    "weekly limit",
    "daily limit",
    "monthly limit",
)


#: Extra in-place retries for the on-device tier when it answers "busy".
#: Every agent's chain ends in one local model served with a small parallel
#: slot count, so during a cloud outage the whole fleet arrives at once and
#: the queue fills. That is backpressure, not death: it drains in seconds,
#: and one retry throws away the only tier still answering.
LOCAL_CAPACITY_RETRIES = 4
LOCAL_CAPACITY_RETRY_JITTER = 3.0

#: Statuses a local inference server uses for "queue full, come back".
_CAPACITY_STATUSES = frozenset({503, 529})


def is_local_model(model: str) -> bool:
    """Is this served on-device, with no credential and no provider account?"""
    return model.startswith(("ollama_chat/", "ollama/"))


def is_capacity_error(e: Exception) -> bool:
    """Is the server saying "busy", rather than "broken"?"""
    if getattr(e, "status_code", None) in _CAPACITY_STATUSES:
        return True
    msg = str(e).lower()
    return any(m in msg for m in ("no slot", "queue is full", "server busy", "overloaded"))


def _status_of(e: Exception) -> int | None:
    return getattr(e, "status_code", None)


def is_credit_exhausted(e: Exception) -> bool:
    """Is the ACCOUNT out of money, rather than the provider merely busy?"""
    if _status_of(e) == 402:
        return True
    msg = str(e).lower()
    return any(marker in msg for marker in _CREDIT_EXHAUSTED_MARKERS)


def _retirement_reason(e: Exception, *, spent: bool) -> Retirement:
    """Which retirement a failed credential earns.

    Split by how the condition RECOVERS, not by how it presents: an auth
    failure never recovers, a spend cap recovers on a top-up, a calendar
    quota recovers when the provider's window rolls.
    """
    if not spent:
        return Retirement.AUTH_FAILED
    if is_periodic_quota_exhausted(e):
        return Retirement.QUOTA_EXHAUSTED_PERIODIC
    return Retirement.CREDIT_EXHAUSTED


def is_periodic_quota_exhausted(e: Exception) -> bool:
    """Is this cap tied to a calendar window rather than a spent balance?

    Prose only, never status: a 402 says the balance is gone, which a
    top-up fixes immediately. Only the provider's own wording distinguishes
    "you are out of money" from "you are out of allowance until Monday",
    and the two need different cooldowns.
    """
    msg = str(e).lower()
    return any(marker in msg for marker in _PERIODIC_QUOTA_MARKERS)


def is_auth_failure(e: Exception) -> bool:
    """Is the CREDENTIAL rejected, as opposed to this model being off-limits?

    401 only, deliberately. A 403 from OpenRouter usually means "this key may
    not use *this model*" — a privileged, moderated, or region-blocked model —
    which is model-specific, not credential-specific. Treating it as a dead key
    would retire a perfectly good credential for the life of the process and
    skip the one model the operator actually needs to hear about.

    Status only, never prose: an existing 403 in the suite carries the message
    "Rate limited", and matching on words would classify it as a live key.
    """
    return _status_of(e) == 401


def rate_limit_wait_seconds(e: Exception) -> float | None:
    """How long to wait before retrying THIS model, or None if not a 429.

    A rate limit is the provider saying "not right now". Marking the model
    broken and walking the fallback chain answers a question it did not ask,
    and burns the primary for the rest of the run.
    """
    status = _status_of(e)
    if status is not None:
        # Trust the status over the prose. A 403 is Forbidden however it is
        # worded — an existing test raises exactly that with the message
        # "Rate limited", and treating it as a wait would retry an auth
        # failure forever instead of falling to the next model.
        if status != 429:
            return None
    elif "rate limit" not in str(e).lower():
        # No status at all: some providers raise bare exceptions, so the
        # message is the only signal available.
        return None
    if is_credit_exhausted(e):
        return None
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    for name in _RETRY_AFTER_HEADERS:
        raw = lowered.get(name)
        if raw is None:
            continue
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return min(seconds, MAX_RATE_LIMIT_WAIT)
    return _DEFAULT_RATE_LIMIT_WAIT


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
    model: str | list[str],
    temperature: float = 0.3,
    json_mode: bool = False,
    timeout: int | float = 120,
    max_retries: int = 1,
    max_tokens: int | None = None,
) -> Any:
    """Single-model LLM call with timeout and optional retry.

    Args:
        messages: Chat messages in OpenAI format.
        model: Model identifier (litellm format), or a chain of them. A
            chain is walked in order and the first model that answers wins,
            which is how a caller outside the agent loop reaches the
            instance's offline tier when the cloud provider is down.
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
    chain = [model] if isinstance(model, str) else list(model)
    if not chain:
        # An empty chain would otherwise fall out of the loop and return
        # None, which every caller here treats as "the model abstained".
        raise ValueError("llm_call: no model to call")

    kwargs: dict[str, Any] = {
        "model": chain[0],
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    async def _attempt() -> Any:
        model = kwargs["model"]
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

    last: Exception | None = None
    for candidate in chain:
        kwargs["model"] = candidate
        try:
            return await retry_async(
                _attempt,
                max_attempts=max_retries,
                retryable_exceptions=_RETRYABLE_EXCEPTIONS,
                backoff_base=1.0,
            )
        except Exception as exc:  # noqa: BLE001 - the next model is the point
            last = exc
            if candidate != chain[-1]:
                logger.warning(
                    "llm_call: %s failed (%s); trying the next model in the chain",
                    candidate,
                    exc,
                )
    assert last is not None  # the loop ran at least once
    raise last


def chain_with_last_resort(model: str) -> list[str]:
    """One model, plus the instance's offline tier if it has one.

    Callers outside the agent loop — the judge, Buddy's reviewer — used to
    name a single cloud model and swallow any failure into ``None``. During
    the 2026-08-26 key cap that produced 90 judge failures and 59 review
    failures in a single hour, with nothing reported: the grading layer went
    dark in exactly the outage it exists to measure, while the local tier
    answering the agent's own turns sat unused.

    ``ROBOTHOR_LAST_RESORT_MODEL`` is the same variable ``_with_last_resort``
    appends to every agent chain, so these callers inherit the fleet's
    offline tier rather than inventing one.
    """
    last_resort = os.environ.get("ROBOTHOR_LAST_RESORT_MODEL", "").strip()
    if not last_resort or last_resort == model:
        return [model]
    return [model, last_resort]


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

    # ─── Credentials ─────────────────────────────────────────────────

    def _key_pool(self, model: str) -> KeyPool | None:
        """The credential pool this model authenticates against, if any.

        Cached per env var so a key retired on one model stays retired for
        the rest of the chain — otherwise every model in a four-deep chain
        pays the dead credential's round trip before advancing.

        Read lazily rather than at construction: the engine loads secrets
        from tmpfs after import, so a pool built in ``__init__`` would be
        permanently empty on a real box.
        """
        var = env_var_for_model(model)
        if var is None:
            return None
        # PROCESS-WIDE, not per-instance. A credential is a property of the
        # process; caching pools on the client meant memory generation kept
        # its own and went on dialling a key this pool had already retired.
        #
        # The pool announces its own death: retire() fires the hook on the
        # exhaustion transition, so one page is emitted per outage instead of
        # one 'credential retired' log line per skipped model (452 of them on
        # 2026-08-27, and no page).
        #
        # None for an unconfigured provider is deliberate: managing an empty
        # pool would mean reporting it "exhausted" and skipping every model on
        # it, when the correct behaviour is the one every deployment has
        # today — let litellm resolve the environment itself.
        from robothor.engine.key_pool import shared_pool
        from robothor.engine.provider_alerts import exhaustion_hook

        return shared_pool(
            var, on_exhausted=exhaustion_hook(var, pool_size=len(keys_from_env(var)))
        )

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
            messages[:] = await maybe_compress(
                messages, models, threshold=compress_threshold, broken_models=broken_models
            )
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
        # A queue-full local tier is backpressure, not a dead model. Marking
        # it broken during a cloud outage removes the last thing answering,
        # for exactly the duration of the load that caused it.
        if is_local_model(model) and is_capacity_error(e) and not is_timeout:
            logger.warning(
                "Local tier %s is at capacity — leaving it in rotation; "
                "this is backpressure, not a model failure.",
                _sanitize(model),
            )
            return
        # Mark broken for auth, rate limit, provider failures, and timeouts
        if broken_models is not None and (
            status in (401, 402, 403, 500, 502, 503, 504) or is_timeout or is_provider_down
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
        # Credentials proven spent during THIS call. A model whose key is
        # in here cannot succeed, so it is skipped rather than tried.
        dead_credentials: set[str] = set()

        for position, model in enumerate(models):
            if broken_models and model in broken_models:
                continue
            model_var = env_var_for_model(model)
            if model_var is not None and model_var in dead_credentials:
                # Its credential was proven spent earlier in this same call.
                # Trying it buys a guaranteed failure and a round trip.
                # Guarded on `is not None` deliberately: a model with no
                # pooled credential shares nothing with anyone, and treating
                # them as a group would let one provider's quota error strand
                # the local tier — the very outage this code exists to end.
                logger.info(
                    "skipping %s — it shares a credential already proven spent",
                    _sanitize(model),
                )
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
            pool = self._key_pool(model)
            if pool is not None and pool.exhausted():
                # Every configured credential for this provider is retired.
                # Calling anyway would omit api_key and hand litellm the very
                # key the pool just proved dead, quietly undoing retirement.
                if model_var is not None:
                    dead_credentials.add(model_var)
                logger.info(
                    "skipping %s — every configured credential for it is retired",
                    _sanitize(model),
                )
                continue
            # Rotating through spare credentials must not eat the transient
            # retry budget: a key swap and a flaky provider are different
            # failures. They are counted separately so that configuring a
            # spare cannot buy extra retries against an unrelated 5xx.
            attempts = 1 + (
                LOCAL_CAPACITY_RETRIES if is_local_model(model) else TRANSIENT_RETRIES_PER_MODEL
            )
            rotations_left = (len(pool) - 1) if pool is not None else 0
            attempt = 0
            while attempt < attempts:
                attempt_key = None
                try:
                    kwargs = self._build_llm_kwargs(
                        model,
                        messages,
                        tools,
                        input_est,
                        temperature,
                        request_timeout=per_call_timeout,
                    )
                    if pool is not None:
                        # Bound per attempt so the failure handler retires the
                        # credential this request actually carried. Re-reading
                        # the pool afterwards retires whatever is current
                        # *then* — which, with the shared client the daemon
                        # builds, is the healthy spare another run just
                        # rotated onto.
                        attempt_key = pool.current()
                        if attempt_key is not None:
                            kwargs["api_key"] = attempt_key
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
                    # A spent budget is not a busy provider: no wait fixes it,
                    # and every other model on the same key fails identically.
                    # Walking the chain just burns them all.
                    # A dead credential is not a dead model. If a spare key
                    # exists, retire this one and retry the SAME model —
                    # advancing the chain would burn models for a reason
                    # that has nothing to do with them, and every one of
                    # them shares the credential that just failed anyway.
                    spent = is_credit_exhausted(e)
                    if (spent or is_auth_failure(e)) and attempt_key is not None:
                        assert pool is not None
                        pool.retire(
                            attempt_key,
                            _retirement_reason(e, spent=spent),
                        )
                        if not pool.exhausted() and rotations_left > 0:
                            rotations_left -= 1
                            logger.warning(
                                "Model %s: credential %s failed (%s) — "
                                "rotating to the next key and retrying "
                                "the same model",
                                _sanitize(model),
                                pool.fingerprint(attempt_key),
                                "credit exhausted" if spent else "auth rejected",
                            )
                            # Deliberately does not advance `attempt`: a key
                            # swap is not a retry of a flaky provider.
                            continue
                    if spent:
                        # Every model sharing this credential will fail the
                        # same way, so they are skipped rather than tried.
                        # But a model on a DIFFERENT credential — or none at
                        # all, like the local ollama tier — is unaffected,
                        # and raising here strands it. On 2026-08-26 that is
                        # exactly what happened: main's chain ended in a
                        # local Qwen that was up and answering in 9.8s, and
                        # a spent OpenRouter key meant the chain never
                        # reached it.
                        if model_var is not None:
                            dead_credentials.add(model_var)
                        # `position`, not models.index(model): a chain may list
                        # the same model twice, and the first index points
                        # behind the cursor at models already tried.
                        reachable = [
                            m
                            for m in models[position + 1 :]
                            if (v := env_var_for_model(m)) is None or v not in dead_credentials
                        ]
                        if reachable:
                            logger.warning(
                                "Model %s: the account's credit is exhausted — "
                                "skipping every model on the same key and "
                                "falling through to %s.",
                                _sanitize(model),
                                _sanitize(reachable[0]),
                            )
                            break
                        logger.error(
                            "Model %s: the account's credit is exhausted — no "
                            "model on this key can answer. Top up or raise the "
                            "limit; this is not a model failure.",
                            _sanitize(model),
                        )
                        raise
                    # A rate limit is the provider saying "not right now".
                    # Wait the interval it names and retry the SAME model.
                    _wait = rate_limit_wait_seconds(e)
                    if _wait is not None and attempt < attempts - 1:
                        logger.warning(
                            "Model %s rate-limited — waiting %.1fs and retrying "
                            "the same model (attempt %d/%d)",
                            _sanitize(model),
                            _wait,
                            attempt + 1,
                            attempts,
                        )
                        if self._active_watchdog:
                            self._active_watchdog.touch(f"rate_limit_wait:{model}")
                        await asyncio.sleep(_wait)
                        attempt += 1
                        continue
                    if attempt < attempts - 1 and _is_transient_model_error(e):
                        if is_local_model(model) and is_capacity_error(e):
                            # The queue drains on its own; a flat, calmer wait
                            # beats hammering the server that is already full.
                            delay = LOCAL_CAPACITY_RETRY_JITTER
                        else:
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
                        attempt += 1
                        continue
                    # Giving up on this model — record the failure once per
                    # chain advancement (a retried-then-recovered blip must
                    # not count double toward the breaker threshold).
                    if is_auth_failure(e):
                        # A rejected credential says nothing about the model.
                        # Blaming it blacklists a healthy model, and because
                        # every model in the chain shares the key, a four-deep
                        # chain blacklists all four — then keeps skipping them
                        # for the breaker's cooldown after the key is fixed.
                        logger.error(
                            "Model %s: the credential was rejected — advancing "
                            "without marking the model broken. Check the key, "
                            "not the provider.",
                            _sanitize(model),
                        )
                        break
                    if is_timeout:
                        breaker.record_failure(model, reason=f"timeout after {per_call_timeout}s")
                        logger.warning(
                            "LLM call to %s exceeded %ds — cancelling and falling back",
                            _sanitize(model),
                            per_call_timeout,
                        )
                    elif not (is_local_model(model) and is_capacity_error(e)):
                        # Local backpressure must not open the breaker: it
                        # would blind the fleet's only offline tier for the
                        # cooldown, during the outage it exists to cover.
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
            if get_model_breaker().is_open(model):
                # _call_llm has skipped open-breaker models for a long time,
                # because a dead provider otherwise costs the full per-call
                # timeout on every run. Streaming is the INTERACTIVE path, so
                # it was the operator's own chat paying that timeout against a
                # provider the engine had already written off. Safe to consult
                # only because a streamed success now reaches the breaker too;
                # otherwise an open breaker could never clear from the one
                # path that proves a model healthy.
                logger.info("skipping %s — circuit breaker open", _sanitize(model))
                continue
            pool = self._key_pool(model)
            if pool is not None and pool.exhausted():
                # Every credential for this provider is retired; calling
                # anyway would omit api_key and let litellm resolve the
                # very key the pool just proved dead.
                logger.info(
                    "skipping %s — every configured credential for it is retired",
                    _sanitize(model),
                )
                continue
            rotations_left = (len(pool) - 1) if pool is not None else 0
            while True:
                attempt_key = None
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
                    if pool is not None:
                        attempt_key = pool.current()
                        if attempt_key is not None:
                            kwargs["api_key"] = attempt_key
                    if is_codex_model(model):
                        async with asyncio.timeout(per_call_timeout):
                            result = await codex_acompletion(**kwargs)
                        content = str(result.choices[0].message.content or "")
                        if on_content and content:
                            await on_content(content)
                        await _emit(
                            {"type": "text_delta", "delta": content, "accumulated": content}
                        )
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
                                        "output_tokens": getattr(usage, "completion_tokens", 0)
                                        or 0,
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
                                            "tool_name": getattr(tc_fn, "name", "")
                                            if tc_fn
                                            else "",
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
                    # The interactive path proves a model healthy more often
                    # than any cron does; without this only failures ever
                    # reach the breaker, so it can open and never clear.
                    get_model_breaker().record_success(model)
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
                    break
                except Exception as e:
                    spent = is_credit_exhausted(e)
                    if (spent or is_auth_failure(e)) and attempt_key is not None:
                        assert pool is not None
                        pool.retire(
                            attempt_key,
                            _retirement_reason(e, spent=spent),
                        )
                        if not pool.exhausted() and rotations_left > 0:
                            rotations_left -= 1
                            logger.warning(
                                "Model %s: credential %s failed (%s) — rotating "
                                "to the next key and retrying the same model",
                                _sanitize(model),
                                pool.fingerprint(attempt_key),
                                "credit exhausted" if spent else "auth rejected",
                            )
                            # Safe to retry in place: a credential error
                            # surfaces at stream creation, before any chunk has
                            # reached on_content, so nothing is duplicated.
                            continue
                    if is_auth_failure(e):
                        # A rejected credential says nothing about the model.
                        logger.error(
                            "Model %s: the credential was rejected — advancing "
                            "without marking the model broken.",
                            _sanitize(model),
                        )
                        last_error = e
                        break
                    self._handle_model_error(e, model, broken_models, streaming=True)
                    last_error = e
                    if self._active_watchdog:
                        self._active_watchdog.touch(f"stream_error_fallback:{model}")
                    break

        # repr(): str(TimeoutError()) is "" — see _call_llm's exhaustion log.
        logger.error("All models failed (streaming). Last error: %s", _sanitize(repr(last_error)))
        return None

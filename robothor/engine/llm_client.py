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
import json
import logging
import math
import random
import time
import time as _time
from collections.abc import Awaitable, Callable  # noqa: TC003
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import litellm

from robothor.engine.codex_provider import CodexProviderError, is_codex_model
from robothor.engine.codex_provider import acompletion as codex_acompletion
from robothor.engine.context import estimate_tokens
from robothor.engine.context_fit import (
    CONTEXT_OVERFLOW_SHRINKS,
    is_context_overflow,
    shrink_after_overflow,
)
from robothor.engine.key_pool import KeyPool, Retirement, env_var_for_model, keys_from_env
from robothor.engine.last_resort import last_resort_attempt
from robothor.engine.llm_attempts import (
    REASONING_ONLY_NUDGE,
    CompletionShape,
    describe_completion,
    note_outcome,
)

# Explicit `as` re-exports: mypy runs with no_implicit_reexport, and callers
# have imported these names from here for a long time. New readers should
# import from llm_budgets directly — a module that only wants to know what 300
# means should not have to load a provider SDK to find out.
from robothor.engine.llm_budgets import (
    BATCH_TRIGGER_TYPES as BATCH_TRIGGER_TYPES,
)
from robothor.engine.llm_budgets import (
    LLM_REQUEST_TIMEOUT as LLM_REQUEST_TIMEOUT,
)
from robothor.engine.llm_budgets import (
    LLM_REQUEST_TIMEOUT_BATCH as LLM_REQUEST_TIMEOUT_BATCH,
)
from robothor.engine.llm_budgets import (
    LLM_REQUEST_TIMEOUT_OLLAMA as LLM_REQUEST_TIMEOUT_OLLAMA,
)
from robothor.engine.llm_budgets import (
    LOCAL_CAPACITY_RETRIES as LOCAL_CAPACITY_RETRIES,
)
from robothor.engine.llm_budgets import (
    TRANSIENT_RETRIES_PER_MODEL as TRANSIENT_RETRIES_PER_MODEL,
)
from robothor.engine.llm_budgets import (
    _timeout_from_env as _timeout_from_env,
)
from robothor.engine.llm_budgets import (
    is_local_model as is_local_model,
)
from robothor.engine.llm_budgets import (
    uses_ollama_timeout as uses_ollama_timeout,
)
from robothor.engine.metrics import LLM_CALL_DURATION, LLM_CALLS_TOTAL, LLM_TOKENS_TOTAL
from robothor.engine.model_breaker import _current_run_id_var, get_model_breaker
from robothor.engine.reasoning_replay import (
    is_reasoning_replay_error,
    merge_streamed_reasoning_details,
    redacted_history_digest,
    strip_reasoning_for_model,
)
from robothor.engine.request_budget import (
    RequestBudgetError,
    RequestRouteUnavailableError,
    bounded_completion,
)
from robothor.engine.required_tool import tool_choice
from robothor.engine.retry import retry_async
from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.stall_watchdog import _active_watchdog_var
from robothor.engine.workflow_budget import bound_call_timeout

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from robothor.engine.session import AgentSession
    from robothor.engine.stall_watchdog import _StallWatchdog

logger = logging.getLogger(__name__)
_response_format_var: ContextVar[str] = ContextVar("agent_response_format", default="text")

# ── LLM request timeouts (shared with runner, which re-exports these) ──
# Max seconds to wait for the next streaming chunk before aborting and
# falling back to the next model. Prevents stalled streams from hanging
# the entire run (the stream *creation* timeout is separate — see
# ``LLMClient._build_llm_kwargs``'s ``timeout``).
STREAM_CHUNK_TIMEOUT = 90


def stream_chunk_timeout(model: str) -> float:
    """Per-chunk stream timeout, sized for the model producing the chunks.

    The flat 90s was calibrated on cloud models. The local 27B's own registry
    entry says ttft_hint_ms=9000 — the one budget here that genuinely is
    per-model, and `model` is already in scope at the chunk loop, so this needs
    no plumbing.
    """
    from robothor.engine.model_registry import tempo_factor

    return STREAM_CHUNK_TIMEOUT * tempo_factor(model)


# HTTP-level timeout passed to litellm.acompletion for the initial
# request (stream creation for streaming, full response for
# non-streaming). Must be longer than STREAM_CHUNK_TIMEOUT so a slow
# first chunk is handled by the per-chunk watchdog rather than killing
# the whole request. Ollama gets more headroom for cold-start loads.


# The per-call allowances and the retry counts live in `llm_budgets`, a leaf
# that pulls in neither litellm nor the rest of the engine. Re-exported here
# because this is where the fleet reads them from, and because the module that
# PREDICTS these numbers (workflow_budget, for "can one step outspend its own
# workflow?") must not have to import a provider SDK to see a 300.
_BATCH_TRIGGER_TYPES = BATCH_TRIGGER_TYPES

# Marker prepended to engine-injected context when it is rewritten from the
# ``developer`` role to a user turn (Anthropic-family models only — see
# ``LLMClient._normalize_developer_role``). Keeps the provenance visible to the
# model so it does not read engine context as operator speech.
ENGINE_CONTEXT_PREFIX = "[engine] "

# A short jitter on the in-place retry so a provider blip is not re-hit
# instantly. The retry COUNT is in llm_budgets; only the timing is here.

# One re-ask per model after a reasoning-only reply (DIAG 2026-09-13 §4.1),
# counted separately from the transient budget: a thinking model that spent its
# budget before the answer started is not a flaky provider, and the re-ask
# carries DIFFERENT kwargs (a smaller budget and a nudge), so spending the
# transient retry on it would leave a genuine 502 with nothing.
REASONING_ONLY_RE_ASKS_PER_MODEL = 1
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


#: Jitter on the on-device tier's capacity retry. The COUNT is in llm_budgets.
LOCAL_CAPACITY_RETRY_JITTER = 3.0


def _local_capacity_delay() -> float:
    """How long to wait after the on-device tier says "busy".

    The queue drains on its own, so a calm wait beats hammering a server that is
    already full. It is SPREAD rather than flat: a credential cap sends the whole
    fleet to the local tier within one tick, and a fixed delay re-synchronises
    every agent onto a single beat, refilling the queue the instant it drains. The
    arrival rate then stays correlated no matter how deep the queue is — which is
    how backpressure became the thermal event of 2026-08-28 rather than absorbing it.
    """
    return random.uniform(LOCAL_CAPACITY_RETRY_JITTER * 0.5, LOCAL_CAPACITY_RETRY_JITTER * 1.5)


#: Statuses a local inference server uses for "queue full, come back".
_CAPACITY_STATUSES = frozenset({503, 529})


def _record_execution_mode(model: str) -> None:
    """Tell the mode tracker what actually served this request.

    Mode is telemetry: it must never be able to fail a call that succeeded, so
    every failure here is swallowed. Called from BOTH breaker success paths --
    streaming and not -- because a signal wired to one is half-blind for agents
    that only ever take the other.
    """
    try:
        from robothor.engine.execution_mode import record_completion

        record_completion(model)
    except Exception:  # pragma: no cover - telemetry must never break a call
        logger.debug("Execution-mode signal failed for %s", model, exc_info=True)


def _per_call_timeout(model: str, timeout_override: float | None) -> float:
    """Seconds one provider call may take before the chain walk gives up on it.

    Wraps each provider call so the runner cancels and falls through if the
    provider hangs: the ``timeout`` kwarg already passed to litellm is
    best-effort and was observed silently ignored, causing 1800s stalls against
    codex/gpt-5.5 in the 2026-05-28 incident. The local tier gets its own,
    larger value; ``timeout_override`` is how batch-shaped (cron/workflow) runs
    get the higher non-interactive allowance.

    Returns the constants UNWRAPPED. They are ints, they reach litellm as the
    ``timeout`` kwarg and they are interpolated into every retry log line, so
    coercing them to float would change both the wire and the operator-facing
    text ("timeout after 600.0s") for no gain.
    """
    if uses_ollama_timeout(model):
        return LLM_REQUEST_TIMEOUT_OLLAMA
    return timeout_override if timeout_override is not None else LLM_REQUEST_TIMEOUT


def _skip_model_reason(
    model: str,
    model_var: str | None,
    dead_credentials: set[str],
    breaker: Any,
    pool_for: Callable[[str], KeyPool | None],
    *,
    ignore_breaker: bool = False,
) -> tuple[str | None, KeyPool | None]:
    """Why this model must not be tried on this call, plus its credential pool.

    Extracted from the chain walk so the admission decision is one readable
    thing rather than three ``continue``s interleaved with the call itself.
    Mutates ``dead_credentials`` on the exhausted-pool branch on purpose: that
    is the record of what this call has already proven spent, and every later
    model sharing the credential is skipped on the strength of it.

    ``pool_for`` is a callable, not a pool, so the lookup stays where it was
    before the extraction — AFTER the two cheap checks. It re-reads the
    provider spec and the environment on every call, and hoisting it above them
    put that (and a documented cold-path database read) onto models the walk
    was about to skip anyway.
    """
    if model_var is not None and model_var in dead_credentials:
        # Its credential was proven spent earlier in this same call. Trying it
        # buys a guaranteed failure and a round trip. Guarded on `is not None`
        # deliberately: a model with no pooled credential shares nothing with
        # anyone, and treating them as a group would let one provider's quota
        # error strand the local tier — the very outage this code exists to end.
        return "it shares a credential already proven spent", None
    if breaker.is_open(model) and not ignore_breaker:
        # This model has failed repeatedly and is in cooldown. Skipping it here
        # is the point: otherwise a dead provider costs the full per-call
        # timeout on every run, forever (codex/* did exactly that for a month).
        #
        # `ignore_breaker` is the ONE exemption, and it is not an amnesty: the
        # last-resort attempt (`last_resort.py`) is by definition the last
        # thing tried, after every model in the chain has already failed. A
        # cooldown is advice about what to try NEXT, and there is no next. Kept
        # open for a run that dialled it: the breaker still guards the ordinary
        # path, which is what stops a dead provider costing every run its
        # timeout.
        return "circuit breaker open", None
    pool = pool_for(model)
    if pool is not None and pool.exhausted():
        # Every configured credential for this provider is retired. Calling
        # anyway would omit api_key and hand litellm the very key the pool just
        # proved dead, quietly undoing retirement.
        if model_var is not None:
            dead_credentials.add(model_var)
        return "every configured credential for it is retired", pool
    return None, pool


async def _releasing_stream(stream: Any, cm: Any) -> Any:
    """Yield a stream's chunks, releasing its inference slot when it ends.

    A streaming call holds the GPU for as long as it is CONSUMED, not for as long
    as ``acompletion`` takes to return. Gating only the call would report the slot
    free while the device was still decoding — which is how three "gated" streams
    reached 90C on 2026-08-28.
    """
    try:
        async for chunk in stream:
            yield chunk
    finally:
        await cm.__aexit__(None, None, None)


async def _gated_acompletion(model: str, kwargs: dict[str, Any]) -> Any:
    """``litellm.acompletion``, holding a local slot for the whole stream.

    Also records which model this task is dialling, so a tool handler further
    down the loop can ask what its own caller is able to see. The streaming
    path has no image-fallback retry (see ``_call_with_image_fallback``), which
    is exactly why the capability has to be knowable BEFORE the call.
    """
    from robothor.engine import model_registry

    model_registry.note_active_model(model)
    if not is_local_model(model):
        return await bounded_completion(litellm.acompletion, **kwargs)

    from robothor.llm.local_gate import Lane, gate

    cm = gate().slot(lane=Lane.NORMAL)
    await cm.__aenter__()
    try:
        stream = await bounded_completion(litellm.acompletion, **kwargs)
    except BaseException:
        await cm.__aexit__(None, None, None)
        raise
    return _releasing_stream(stream, cm)


@asynccontextmanager
async def _local_slot(model: str, lane: Any = None) -> AsyncIterator[None]:
    """Hold a local inference slot for one call; a no-op for cloud models.

    Cloud fan-out stays unguarded — someone else's datacentre is not our heat
    budget. On-device work is rationed in watts (docs/instance/THERMAL.md).

    A refusal raises ``LocalCapacityBusyError`` (status 503), which
    ``is_capacity_error`` below already recognises, so it routes into the existing
    LOCAL_CAPACITY_RETRIES path and deliberately does NOT open the breaker.
    """
    if not is_local_model(model):
        yield
        return
    from robothor.llm.local_gate import Lane, gate

    async with gate().slot(lane=lane or Lane.NORMAL):
        yield


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


#: Answer room a thinking block must leave inside ``max_tokens``. Reasoning
#: tokens count against the SAME ceiling on OpenRouter, so a turn that spends
#: its budget before content starts comes back reasoning-only with
#: ``finish_reason=length`` — measured 2026-09-13 as ~47 wasted generations a
#: day, a median of 8s each. Nothing compared the two numbers before this.
MIN_ANSWER_TOKENS = 4_096

#: Anthropic rejects a thinking block below 1,024 tokens. An answer budget too
#: small to host one gets no thinking block at all rather than an invalid one.
MIN_THINKING_BUDGET = 1_024


#: Share of one completion each reasoning-effort rung may spend on thinking.
#: A FRACTION, not a token count: every ``supports_thinking`` model in the
#: fleet requests the same 16,384 default output, so absolute rungs
#: (10k/24k/48k) all clamped to one ceiling and three of the four manifest
#: settings were indistinguishable on the wire — the knob looked armed and
#: moved nothing above ``low``.
#: NOTE: on the current fleet `high` and `max` resolve to the SAME budget —
#: 75% of 16,384 is already the MIN_ANSWER_TOKENS cap — so `max` buys nothing
#: over `high` until a model with a larger default_output_tokens is registered.
_EFFORT_THINKING_SHARE: dict[str, float] = {
    "low": 0.25,
    "medium": 0.50,
    "high": 0.75,
    "max": 0.90,
}


def _thinking_budget(max_tokens: int, effort: str, *, reduced: bool = False) -> int:
    """Thinking tokens for one call: a share of ``max_tokens``, with headroom.

    Monotone in BOTH arguments, which is the property that matters: a higher
    effort never buys less thinking, and a larger completion ceiling never does
    either. (The first cut clamped an absolute budget and managed to give
    ``max_tokens=5120`` a SMALLER budget than 4096.)

    ``reduced`` is the re-ask after a reasoning-only reply: half the share, and
    never above the ``low`` rung, so it is strictly smaller than whatever the
    attempt that came back answerless had asked for.
    """
    share = _EFFORT_THINKING_SHARE.get((effort or "medium").strip().lower(), 0.5)
    if reduced:
        share = min(share / 2, _EFFORT_THINKING_SHARE["low"])
    # The answer keeps MIN_ANSWER_TOKENS, or half the completion when the
    # ceiling is too small to give them — reasoning tokens count against the
    # SAME ceiling on OpenRouter, and a turn that spends it before content
    # starts comes back reasoning-only with `finish_reason=length`.
    answer_floor = min(MIN_ANSWER_TOKENS, max_tokens // 2)
    return max(0, min(int(share * max_tokens), max_tokens - answer_floor))


def _thinking_kwargs(model: str, max_tokens: int, *, reduced: bool = False) -> dict[str, Any]:
    """The thinking block for one call, sized against the answer it leaves room for.

    The effort is the running agent's own (``model_registry``'s per-run
    ContextVar, set at ``runner.py:628-630``). Until 2026-09-13 this read the
    bare ``THINKING_BUDGET_TOKENS`` constant, so the per-agent setting reached
    nothing and every agent on the fleet reasoned at ``medium``.

    ``temperature`` is forced only for the Anthropic family, which is what the
    API that requires it actually is. The comment saying so outlived its code
    when ``supports_thinking`` was extended past Anthropic, and the fleet's
    dedup and classification work had been sampling at maximum entropy since.
    """
    from robothor.engine.model_registry import current_reasoning_effort

    budget = _thinking_budget(max_tokens, current_reasoning_effort(), reduced=reduced)
    if budget < MIN_THINKING_BUDGET:
        logger.debug(
            "no thinking block for %s: max_tokens=%d leaves no usable budget",
            _sanitize(model),
            max_tokens,
        )
        return {}
    block: dict[str, Any] = {"thinking": {"type": "enabled", "budget_tokens": budget}}
    if LLMClient._is_anthropic_family(model):
        block["temperature"] = 1.0  # Anthropic rejects thinking at any other value
    return block


def thinking_kwargs_for_call(
    model: str, max_tokens: int, *, reduced: bool = False
) -> dict[str, Any]:
    """Thinking kwargs for one call, or ``{}`` when this model does not reason.

    The gate and the block together — the only entry point any caller outside
    this module should use. Compaction used to build its own re-ask knob, a
    top-level ``reasoning_effort`` litellm maps per route: for
    ``openrouter/xiaomi/mimo-v2.5`` it maps nothing, so the call raised
    ``UnsupportedParamsError`` before it left the process and compaction fell
    through to the chain walk its re-ask exists to prevent (journal 2026-09-13
    19:45 ET). DeepSeek, which litellm does map, hid it. Asking here instead
    means a model's reasoning support is read from the registry once and the
    payload shape is whatever ``_thinking_kwargs`` emits — there is no second
    implementation to drift.
    """
    from robothor.engine.model_registry import get_model_limits

    if not get_model_limits(model).supports_thinking:
        return {}
    return _thinking_kwargs(model, max_tokens, reduced=reduced)


#: Shortest attempt worth dialling. A model whose shared allowance has less
#: than this left advances the chain instead: the call would be cancelled
#: mid-generation and the wall clock spent for certain. Always read through
#: :func:`_attempt_floor` — a deployment that sets a per-call timeout SHORTER
#: than this (the test suite does, and so would a tuned interactive path) must
#: still get its first attempt.
MIN_ATTEMPT_SECONDS = 5.0

#: Longest fallback chain the wall-clock arithmetic below is checked against.
#: Not enforced — a chain is an operator's choice — but a longer one is logged,
#: because the 1800s `thread_pool.PENDING_EXPIRY_SECONDS` a sub-agent turn is
#: expected to fit inside is verified against exactly this number.
MAX_CHAIN_MODELS_BUDGETED = 5


def worst_case_dispatch_seconds(models: int, per_call_timeout: float) -> float:
    """Ceiling on the wall clock one dispatch can spend.

    Each model may spend at most ``per_call_timeout`` ACROSS all of its
    attempts (see ``_attempt_timeout``) plus the jitter slept between them, so
    the chain's worst case is that, times the number of models. Before the
    shared allowance this was ``attempts × per_call_timeout`` per model — on a
    batch trigger, 3 × 300s × 4 models = an hour for one dispatch, with the
    run's wall-clock ceiling checked only BETWEEN loop iterations.
    """
    return models * (per_call_timeout + TRANSIENT_RETRIES_PER_MODEL * TRANSIENT_RETRY_JITTER_MAX)


def _warn_if_chain_outgrows_its_budget(models: list[str]) -> None:
    """Say so when a chain is longer than the wall-clock arithmetic assumes.

    ``worst_case_dispatch_seconds`` is verified against
    ``MAX_CHAIN_MODELS_BUDGETED``; a longer chain can outlast the
    pending-expiry window a sub-agent turn is expected to fit inside, and an
    unobservable assumption is how a budget stops being one.
    """
    if len(models) > MAX_CHAIN_MODELS_BUDGETED:
        logger.warning(
            "chain of %d models exceeds the %d the wall-clock budget is checked against",
            len(models),
            MAX_CHAIN_MODELS_BUDGETED,
        )


def _attempt_floor(per_call_timeout: float) -> float:
    """Least time an attempt may be given before the chain advances instead."""
    return min(MIN_ATTEMPT_SECONDS, per_call_timeout)


def _attempt_timeout(model_deadline: float, per_call_timeout: float) -> float:
    """Seconds this attempt may take: what is left of the model's allowance.

    Whole seconds, whichever clamp binds. These numbers reach litellm as the
    ``timeout`` kwarg and are interpolated into operator-facing log lines, and a
    fifteen-decimal float there is not an improvement on "timeout after 600s".

    ONE rule for both inputs — round up, then take the smaller. Up, because the
    first attempt on a model must get its full allowance rather than the
    microseconds less that reading the clock costs, and because a workflow
    remainder of 0.4s has to stay a call that HAPPENS: bounded at a second and
    cancelled, so the deadline surfaces named on the next pass through the loop
    rather than the chain quietly ending with nothing tried. The overshoot is
    under a second against budgets of minutes, and the workflow's own outer
    timeout is still the backstop.
    """
    allowance = math.ceil(model_deadline - time.monotonic())
    return float(min(math.ceil(per_call_timeout), allowance))


def _retry_delay(
    e: Exception,
    model: str,
    model_deadline: float,
    per_call_timeout: float,
) -> float | None:
    """Seconds to wait before an in-place retry, or None for "do not retry".

    None when the failure is not transient, and also when this model's shared
    time allowance cannot fund another attempt — a second 300s call on a model
    that has already spent 300s is how one dispatch outgrows the run ceiling.
    """
    if not _is_transient_model_error(e):
        return None
    if is_local_model(model) and is_capacity_error(e):
        delay = _local_capacity_delay()
    else:
        delay = random.uniform(TRANSIENT_RETRY_JITTER_MIN, TRANSIENT_RETRY_JITTER_MAX)
    # The allowance covers PROVIDER time; the backoff is counted separately by
    # worst_case_dispatch_seconds. So the question is only whether another
    # attempt could still say anything before this model's allowance runs out.
    if _attempt_timeout(model_deadline, per_call_timeout) < _attempt_floor(per_call_timeout):
        logger.warning(
            "Model %s: its per-model time allowance is spent — advancing "
            "instead of retrying in place",
            _sanitize(model),
        )
        return None
    return delay


def _rotate_credential(
    model: str,
    pool: KeyPool,
    attempt_key: str,
    e: Exception,
    *,
    spent: bool,
    rotations_left: int,
) -> bool:
    """Retire the credential this attempt carried; True when a spare took over.

    The retirement happens either way — that is the record that this key is
    dead. Only the rotation is conditional.
    """
    pool.retire(attempt_key, _retirement_reason(e, spent=spent))
    if pool.exhausted() or rotations_left <= 0:
        return False
    logger.warning(
        "Model %s: credential %s failed (%s) — rotating to the next key and "
        "retrying the same model",
        _sanitize(model),
        pool.fingerprint(attempt_key),
        "credit exhausted" if spent else "auth rejected",
    )
    return True


async def _emit_usage(chunk: Any, emit: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    """Forward a token count that arrived on a chunk carrying no choices.

    Several providers report usage that way at the end of a stream, and it is
    the only place the count exists — a stream whose last frame is dropped
    bills the run at zero.
    """
    usage = getattr(chunk, "usage", None)
    if not usage:
        return
    await emit(
        {
            "type": "usage",
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }
    )


def _rotated_for(
    e: Exception,
    model: str,
    pool: KeyPool | None,
    attempt_key: str | None,
    *,
    spent: bool,
    rotations_left: int,
) -> bool:
    """Did a spare credential take over for this failure? Then re-ask in place.

    A dead credential is not a dead model. If a spare exists, retire this one
    and retry the SAME model — advancing the chain would burn models for a
    reason that has nothing to do with them, and every one of them shares the
    credential that just failed anyway.

    Both call paths ask the identical question, and asked it in two hand-copied
    blocks; on the streaming path the in-place retry is safe for a reason worth
    keeping written down — a credential failure surfaces at stream CREATION,
    before any chunk has reached ``on_content``, so nothing is duplicated.
    """
    if not (spent or is_auth_failure(e)) or attempt_key is None or pool is None:
        return False
    return _rotate_credential(
        model, pool, attempt_key, e, spent=spent, rotations_left=rotations_left
    )


def _streaming_skip_reason(model: str, pool: KeyPool | None) -> str | None:
    """Why this model must not be dialled on the streaming path.

    The breaker check is here for the same reason ``_call_llm`` has one: a dead
    provider otherwise costs the full per-call timeout on every run, and
    streaming is the INTERACTIVE path, so it was the operator's own chat paying
    it against a provider the engine had already written off. Safe to consult
    only because a streamed success now reaches the breaker too; otherwise an
    open breaker could never clear from the one path that proves a model
    healthy. An exhausted pool is the other: calling anyway would omit api_key
    and let litellm resolve the very key the pool just proved dead.
    """
    if get_model_breaker().is_open(model):
        return "circuit breaker open"
    if pool is not None and pool.exhausted():
        return "every configured credential for it is retired"
    return None


def _advance_without_blaming_the_model(e: Exception, model: str) -> bool:
    """True when the chain must advance WITHOUT marking this model broken.

    A rejected credential says nothing about the model. Blaming it blacklists a
    healthy model, and because every model in the chain shares the key, a
    four-deep chain blacklists all four — then keeps skipping them for the
    breaker's cooldown after the key is fixed.
    """
    if not is_auth_failure(e):
        return False
    logger.error(
        "Model %s: the credential was rejected — advancing without marking the "
        "model broken. Check the key, not the provider.",
        _sanitize(model),
    )
    return True


def _streamed_shape(
    rebuilt: Any,
    accumulated_content: str,
    has_tool_calls: bool,
) -> CompletionShape:
    """What the turn actually DELIVERED, not only what the rebuild says.

    ``litellm.stream_chunk_builder`` can hand back a blank message for a stream
    whose deltas already reached ``on_content`` — the operator has watched that
    answer arrive. Judging emptiness from the rebuild alone would advance the
    chain and replace a delivered turn with a different model's, and record a
    failed attempt for a call that worked. The live locals are the truth about
    delivery, so they override the rebuild; nothing else about the shape (the
    finish reason, the token counts, the reasoning fields) is touched.
    """
    shape = describe_completion(rebuilt)
    if not accumulated_content and not has_tool_calls:
        return shape
    return replace(
        shape,
        parsed=True,
        content_present=bool(accumulated_content),
        tool_calls_present=has_tool_calls,
    )


def _streamed_without_an_answer(model: str, shape: CompletionShape) -> EmptyCompletionError:
    """Advance the chain on a streamed reply that carried no answer.

    The streaming path had NO emptiness check: it recorded the attempt and
    returned the response, so one provider call left a failed-attempt row AND a
    success row — the second claiming an answer that was never there. It also
    handed the operator a blank turn where the non-streaming path would have
    fallen through to the next model.

    No in-place retry here, unlike ``_call_llm``: a retry after partial content
    has already been emitted to ``on_content`` would show duplicated text. That
    cannot arise for THIS branch — an answerless reply emitted no content — but
    the rule stays simple, so the chain advances instead.
    """
    error = EmptyCompletionError(
        f"{model} streamed no content and no tool call ({shape.describe()})"
    )
    logger.warning(
        "Model %s streamed %s (%s) — advancing to the next model",
        _sanitize(model),
        shape.outcome,
        _sanitize(shape.describe()),
    )
    get_model_breaker().record_failure(model, reason=f"streamed {shape.outcome}")
    return error


def _blame_model(breaker: Any, e: Exception, model: str, attempt_timeout: float) -> None:
    """Record the failure against the model, once per chain advancement.

    A retried-then-recovered blip must not count double toward the breaker
    threshold, so this is called only where the chain actually advances.
    """
    if isinstance(e, TimeoutError):
        breaker.record_failure(model, reason=f"timeout after {attempt_timeout}s")
        logger.warning(
            "LLM call to %s exceeded %ds — cancelling and falling back",
            _sanitize(model),
            attempt_timeout,
        )
    elif not (is_local_model(model) and is_capacity_error(e)):
        # Local backpressure must not open the breaker: it would blind the
        # fleet's only offline tier for the cooldown, during the outage it
        # exists to cover.
        breaker.record_failure(model, reason=str(e)[:120])


def _spent_credit_leaves_someone_reachable(
    models: list[str],
    position: int,
    model: str,
    model_var: str | None,
    dead_credentials: set[str],
) -> bool:
    """Record a spent credential and say whether the chain can still answer.

    Every model sharing this credential will fail the same way, so they are
    skipped rather than tried. But a model on a DIFFERENT credential — or none
    at all, like the local ollama tier — is unaffected, and raising strands it.
    On 2026-08-26 that is exactly what happened: main's chain ended in a local
    Qwen that was up and answering in 9.8s, and a spent OpenRouter key meant
    the chain never reached it.

    True → fall through to the next reachable model. False → nobody can answer.
    """
    if model_var is not None:
        dead_credentials.add(model_var)
    # `position`, not models.index(model): a chain may list the same model
    # twice, and the first index points behind the cursor at models already
    # tried.
    reachable = [
        m
        for m in models[position + 1 :]
        if (v := env_var_for_model(m)) is None or v not in dead_credentials
    ]
    if reachable:
        logger.warning(
            "Model %s: the account's credit is exhausted — skipping every "
            "model on the same key and falling through to %s.",
            _sanitize(model),
            _sanitize(reachable[0]),
        )
        return True
    logger.error(
        "Model %s: the account's credit is exhausted — no model on this key "
        "can answer. Top up or raise the limit; this is not a model failure.",
        _sanitize(model),
    )
    return False


def _log_reasoning_only(model: str, shape: CompletionShape) -> None:
    """Name the reasoning-only case distinctly from a provider empty.

    They read identically in the journal otherwise, which is how ~47 of 50
    "returned no content" events a day went a week without a diagnosis.
    """
    logger.warning(
        "Model %s returned reasoning_only (%s) — re-asking the same model once "
        "with a reduced thinking budget and a nudge for the answer",
        _sanitize(model),
        _sanitize(shape.describe()),
    )


def _is_transient_model_error(e: BaseException) -> bool:
    """True for failures worth one same-model retry: timeouts, 5xx, empties."""
    if isinstance(e, TimeoutError | EmptyCompletionError):
        return True
    return getattr(e, "status_code", None) in _TRANSIENT_RETRY_STATUSES


#: Retries for a model that emitted tool-call JSON nobody can parse. ONE: a
#: truncated generation is worth re-rolling once, but the failure is usually
#: deterministic — the unparseable arguments are already in the request being
#: transformed — and spending the local tier's whole retry budget on it is how
#: twelve parse errors became twelve dead runs.
MALFORMED_TOOL_ARGS_RETRIES = 1

#: What `json.JSONDecodeError` says when a payload stops MID-STRUCTURE. litellm
#: parses a tool call's `arguments` inside its provider transformations and
#: re-raises the decode failure as an `APIConnectionError` — status 500,
#: indistinguishable by status from a provider that is actually down. These
#: strings are the only thing left that tells the two apart, and a truncated
#: generation is well-formed right up to where it stopped, so this is the shape
#: it decodes to.
#:
#: `expecting value: line` is deliberately ABSENT. That is what an empty body
#: or an HTML error page decodes to, never a truncated arguments blob — and
#: litellm parses the RESPONSE with the same module (`raw_response.json()`,
#: `json.loads(response_json_message["content"])`), re-raising it as the same
#: exception. While it was listed here, an ollama that had fallen over behind a
#: proxy was read as bad output: re-rolled, skipped, breaker never told, for as
#: long as the outage lasted.
_JSON_DECODE_MARKERS = (
    "unterminated string starting at",
    "expecting ',' delimiter",
    "expecting ':' delimiter",
    "expecting property name enclosed in double quotes",
    "invalid control character at",
    "invalid \\escape",
    "extra data: line",
)

#: Words that place a decode failure on the REQUEST side. litellm reaches
#: `tool_call["function"]["arguments"]` to build the request; a response-side
#: parse names neither. Checked against every message in the cause chain and
#: against the traceback's frame names, both of which are often absent — this
#: only ever ADDS confidence, it is never required.
_TOOL_ARGUMENT_HINTS = ("arguments", "tool")


def _exception_chain(e: BaseException) -> list[BaseException]:
    """`e` and everything it was raised from, cycle-guarded."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    cause: BaseException | None = e
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        chain.append(cause)
        cause = cause.__cause__ or cause.__context__
    return chain


def _references_tool_arguments(chain: list[BaseException]) -> bool:
    """True if anything in the chain names a tool call or its arguments.

    Frame NAMES only, not filenames: a test module about tool arguments is
    named after them, and matching the filename would make every such test
    self-confirming.
    """
    for exc in chain:
        if any(hint in str(exc).lower() for hint in _TOOL_ARGUMENT_HINTS):
            return True
        tb = exc.__traceback__
        while tb is not None:
            if any(hint in tb.tb_frame.f_code.co_name.lower() for hint in _TOOL_ARGUMENT_HINTS):
                return True
            tb = tb.tb_next
    return False


def is_malformed_tool_arguments(e: BaseException) -> bool:
    """True when the model's tool-call arguments could not be parsed.

    This is BAD OUTPUT, not a provider outage. Observed 12 times in 24 hours
    on the on-device tier: `litellm.APIConnectionError: Unterminated string
    starting at: line 1 column 3175`, raised from `json.loads` inside litellm's
    ollama chat transformation. Read as a 500, it burned the local retry budget
    and then opened the breaker on the tier every agent's chain ends in.

    Deliberately narrow, and narrower than the exception type allows. A
    `JSONDecodeError` in the cause chain is NOT on its own enough: litellm
    parses the response with the same module it parses the request with, so a
    provider returning an empty body or an HTML error page raises the same
    exception from the same place. The two are otherwise indistinguishable —
    nothing in the type, the status, or the provider tells them apart — so the
    decoder's own words carry the verdict, backed by any mention of a tool call
    or its arguments in the chain's messages or frame names.

    A payload that stops mid-structure ("Unterminated string starting at",
    "Expecting ',' delimiter") is a truncated generation. "Expecting value:
    line 1 column 1" is an empty or non-JSON body, and stays a provider
    failure: the cost of guessing wrong there is a breaker that never opens on
    a real outage.
    """
    chain = _exception_chain(e)
    has_decoder = any(isinstance(c, json.JSONDecodeError) for c in chain)
    if not has_decoder and not isinstance(e, litellm.exceptions.APIConnectionError):
        return False
    text = " ".join(str(c) for c in chain).lower()
    if any(marker in text for marker in _JSON_DECODE_MARKERS):
        return True
    return has_decoder and _references_tool_arguments(chain)


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


async def _emit_tool_call_events(
    tool_calls: list[Any],
    seen_tool_ids: set[str],
    emit: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    """Emit ``tool_use_start`` once per call id, then its argument deltas.

    A streamed tool call arrives as one id-bearing chunk followed by argument
    fragments, so the start event must fire exactly once per id — ``seen_tool_ids``
    is the caller's set and is updated here.
    """
    for tc in tool_calls:
        tc_id = getattr(tc, "id", None)
        tc_fn = getattr(tc, "function", None)
        if tc_id and tc_id not in seen_tool_ids:
            seen_tool_ids.add(tc_id)
            await emit(
                {
                    "type": "tool_use_start",
                    "tool_name": getattr(tc_fn, "name", "") if tc_fn else "",
                    "call_id": tc_id,
                }
            )
        if tc_fn and getattr(tc_fn, "arguments", None):
            await emit(
                {
                    "type": "tool_use_delta",
                    "delta": tc_fn.arguments,
                    "call_id": tc_id or "",
                }
            )


def _log_reasoning_replay_rejection(
    model: str,
    e: BaseException,
    messages: list[dict[str, Any]] | None = None,
) -> None:
    """Name a thinking-mode replay rejection, or say nothing.

    A history replayed without the provider's own reasoning arrives as a
    generic 400 "Provider returned error", indistinguishable in the journal
    from a dozen other bad requests — on 2026-09-11 that cost the fleet a day
    on the local tier with nothing pointing at the cause. Every path that
    swallows a model error calls this.

    ``messages`` adds a redacted digest of the rejected history. Replaying the
    obvious variants against the live provider returned 200 every time, so the
    shape that actually 400s is still unidentified and only the rejected
    conversation can name it — shapes and sizes only, never message text.
    """
    if not is_reasoning_replay_error(e):
        return
    logger.error(
        "Model %s rejected the conversation because the assistant turn's "
        "reasoning was not echoed back (thinking mode requires it) — "
        "reasoning_replay_rejected=True: %s",
        _sanitize(model),
        _sanitize(e),
    )
    if messages is None:
        return
    # The digest is diagnosis, not control flow: a run must not die, and the
    # rejection above must not go unlogged, because a history had a shape this
    # sketch could not walk.
    try:
        logger.error(
            "reasoning_replay_history %s", _sanitize(redacted_history_digest(messages, model))
        )
    except Exception as digest_error:  # noqa: BLE001 — never lose the real error
        logger.warning("reasoning-replay history digest failed: %s", _sanitize(digest_error))


async def llm_call(
    messages: list[dict[str, Any]],
    *,
    model: str | list[str],
    temperature: float = 0.3,
    json_mode: bool = False,
    timeout: int | float = 120,
    max_retries: int = 1,
    max_tokens: int | None = None,
    api_key: str | None = None,
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
        api_key: Credential for THIS call only. Without it litellm resolves the
            process environment, which is correct for every normal caller and
            wrong for the one that has to validate a key the operator has just
            typed and not yet stored — putting that key in ``os.environ`` would
            hand it to every thread and subprocess for the life of the process.

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
    if api_key is not None:
        kwargs["api_key"] = api_key

    async def _attempt() -> Any:
        model = kwargs["model"]
        t0 = _time.monotonic()
        try:
            call = codex_acompletion if is_codex_model(model) else litellm.acompletion
            resp = await asyncio.wait_for(bounded_completion(call, **kwargs), timeout=timeout)
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
        # Callers here pass their own message lists, but a caller replaying an
        # agent's history would carry engine bookkeeping and another provider's
        # reasoning into this payload. No-op for everything else.
        kwargs["messages"] = strip_reasoning_for_model(messages, candidate)
        try:
            return await retry_async(
                _attempt,
                max_attempts=max_retries,
                retryable_exceptions=_RETRYABLE_EXCEPTIONS,
                backoff_base=1.0,
            )
        except RequestRouteUnavailableError as exc:
            last = exc
            logger.info("No eligible funded route for %s — advancing", _sanitize(candidate))
        except RequestBudgetError:
            raise
        except Exception as exc:  # noqa: BLE001 - the next model is the point
            last = exc
            # Judge, buddy review and the background legs never touch
            # _handle_model_error, so without this the same rejection is
            # silent on every path outside the agent loop.
            _log_reasoning_replay_rejection(candidate, exc, kwargs["messages"])
            if candidate != chain[-1]:
                logger.warning(
                    "llm_call: %s failed (%s); trying the next model in the chain",
                    _sanitize(candidate),
                    _sanitize(exc),
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

    The name comes from :func:`robothor.engine.config.last_resort_model`, the
    same reader ``_with_last_resort`` uses for every agent chain, so these
    callers inherit the fleet's offline tier rather than inventing one -- on
    a ``genus init`` instance the name lives in ``config.yaml``, not in the
    environment, and a raw ``ROBOTHOR_LAST_RESORT_MODEL`` read would miss it.
    """
    from robothor.engine.config import last_resort_model

    last_resort = last_resort_model()
    if not last_resort or last_resort == model:
        return [model]
    return [model, last_resort]


#: What the agent is told in place of a picture its model cannot see. Plain
#: text, in the tool result where the image would have been, so the agent
#: learns the capability is unavailable *for this model* and can fall back to
#: inspecting the file programmatically.
IMAGE_UNSUPPORTED_NOTE = (
    "[the image could not be shown to this model — it accepts text only. "
    "Call view_image again on the path above: the refusal has been recorded, "
    "so this time you get the local vision model's description instead of a "
    "picture that would only be stripped again. Or inspect the file "
    "programmatically, e.g. with Pillow via exec.]"
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


def _bind_attempt_key(pool, kwargs):
    """Bind and return the credential this attempt actually uses.

    Failure handling must retire this key, even if a concurrent run rotates the
    shared pool before the response arrives. None leaves existing kwargs intact.
    """
    key = pool.current() if pool is not None else None
    if key is not None:
        kwargs["api_key"] = key
    return key


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

    @contextlib.contextmanager
    def _watchdog_wait(self, label: str, budget: float) -> Any:
        """Tell the watchdog this await is already bounded by `budget` seconds.

        The whole point is that the run loop emits NO touch while a
        non-streaming provider call is in flight, so a first token the engine
        itself allows 600s for read as a 120s stall (2026-08-27, fleet-wide).

        A context manager rather than a keepalive task: nothing to leak, no
        interaction with the watchdog's own task.cancel(), no create_task GC
        hazard, and exception-safe across an except arm with five exits.
        No-ops when nothing is watching (sub-agents, out-of-loop callers).
        """
        wd = self._active_watchdog
        if wd is None:
            yield
            return
        wd.begin_wait(label, budget)
        try:
            yield
        finally:
            wd.end_wait()

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
        from robothor.engine.key_pool import provider_for_var, resolve_keys, shared_pool
        from robothor.engine.provider_alerts import exhaustion_hook

        # Counted the way the pool is BUILT, not the way it used to be: a
        # vault-only provider has no numbered env siblings at all, so
        # keys_from_env reported a pool of zero and the exhaustion page said
        # "0 of 0 credentials" for an outage of four real keys.
        spec = provider_for_var(var)
        size = len(resolve_keys(spec.id)) if spec is not None else len(keys_from_env(var))
        return shared_pool(var, on_exhausted=exhaustion_hook(var, pool_size=size))

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
        format_token = _response_format_var.set(
            "json_object"
            if getattr(session, "response_format", "text") == "json_object"
            else "text"
        )
        try:
            from robothor.engine.provider_routing import provider_order_scope

            with provider_order_scope(getattr(session, "provider_order", {})):
                if on_content or on_stream_event:
                    response = await self._call_llm_streaming(
                        session.messages,
                        models,
                        tool_schemas,
                        on_content,
                        broken_models=broken_models,
                        temperature=temperature,
                        on_stream_event=on_stream_event,
                        timeout_override=timeout_override,
                    )
                else:
                    response = await self._call_llm(
                        session.messages,
                        models,
                        tool_schemas,
                        broken_models=broken_models,
                        temperature=temperature,
                        timeout_override=timeout_override,
                    )
                if response is not None:
                    return response
                # Every model in the chain is out. The local tier is the reason a
                # last fallback exists, and the commonest reason a chain ends with
                # nothing is a conversation that outgrew it — so the one thing not
                # yet tried is the SHORTEST possible conversation. Unstreamed and
                # untooled: all that is left to produce is a sentence for whoever
                # is waiting. (2026-09-16: the local model had answered twelve
                # steps of the run that ended "All models failed to respond".)
                return await last_resort_attempt(self, session, models)
        finally:
            _current_run_id_var.reset(run_token)
            _response_format_var.reset(format_token)

    # ─── Pre-flight ──────────────────────────────────────────────────

    @staticmethod
    def sizing_model(models: list[str], broken_models: set[str] | None = None) -> str:
        """Return the model the context-window math should be sized against.

        G2b: this is the model the fallback loop will actually try next, not
        ``models[0]`` (the configured primary). When the primary is down and
        the run is on a smaller-window fallback, sizing against the primary's
        window (e.g. 1M) can overflow the fallback (e.g. 64K).

        "Will actually try next" is decided by :func:`context_fit.
        next_reachable_model`, which asks the same three questions the chain
        walk asks — broken, breaker open, credential pool spent. The version
        that asked only about ``broken_models`` sized a run living entirely on
        the local tier against the primary it had never once reached, because a
        spent credential never marks a model broken (2026-09-16).
        """
        from robothor.engine.context_fit import next_reachable_model

        return next_reachable_model(models, broken_models)

    async def _prepare_llm_call(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        broken_models: set[str] | None = None,
    ) -> int:
        """Shared pre-flight: compress context and estimate input tokens.

        Mutates messages in-place. Returns estimated input token count.

        This is the ONLY budget the first call of a run gets: the loop's own
        pass returns early on iteration 0, and a run resumed from a journal or
        a long chat history starts with the whole conversation already in hand.
        So it does what the loop does — size against the model that will
        actually be reached, compact above the threshold, and then ENFORCE the
        ceiling deterministically. Sizing here used the flat ``chars / 4`` and
        never touched the ceiling, which shipped ~110k estimated tokens at a
        65,536-token model with no drop and no note (hostile review, M4).
        """
        from robothor.engine.context import maybe_compress
        from robothor.engine.context_fit import enforce_ceiling, estimate_for, fit_for

        fit = fit_for(self.sizing_model(models, broken_models))
        try:
            # Same clamped threshold as the in-loop trigger. The old
            # 0.75-of-window guard was 786K tokens on the fleet primary's 1M
            # window — unreachable, zero firings in 7 days. When the in-loop
            # pass just compacted, this no-ops (estimate is under threshold);
            # it exists for the paths that call the client without the loop.
            messages[:] = await maybe_compress(
                messages, models, threshold=fit.threshold, broken_models=broken_models
            )
        except Exception as e:
            logger.warning("Pre-flight compression failed: %s", _sanitize(e))
        # Outside the try on purpose: compaction summarises with a MODEL, and
        # the call that most needs the ceiling is the one whose summariser
        # could not reach one. `enforce_ceiling` never raises.
        enforce_ceiling(messages, fit)

        return estimate_for(messages, fit.model)

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

        marker_text = ENGINE_CONTEXT_PREFIX.strip()
        has_engine_turn = any(m.get("role") == ENGINE_CONTEXT_ROLE for m in messages)
        has_typed_marker = any(
            m.get("role") == "user" and marker_text in str(m.get("content") or "") for m in messages
        )
        if not has_engine_turn and not has_typed_marker:
            return messages

        # The prefix is for EVERY provider, not only Anthropic. Live on
        # 2026-09-14 (DeepSeek via OpenRouter) the main agent opened every
        # reply with a "security flag" that the operator's message "arrived
        # wrapped in injected blocks" — the engine's own plan, working state
        # and identity turns, folded next to the user text without a name on
        # them. Behavioural rule 6 then made the agent report its own engine
        # as an attacker. Only the ROLE rewrite stays Anthropic-specific.
        to_user = LLMClient._is_anthropic_family(model)
        marker = ENGINE_CONTEXT_PREFIX.strip()
        normalized: list[dict[str, Any]] = []
        converted = 0
        for msg in messages:
            if msg.get("role") == "user":
                # The label the rules trust must not be typeable: a user turn
                # that opens a line with the marker is defanged, so a chat
                # message cannot impersonate the engine.
                normalized.append(LLMClient._defang_user_marker(msg, marker))
                continue
            if msg.get("role") != ENGINE_CONTEXT_ROLE:
                normalized.append(msg)
                continue
            converted += 1
            rewritten = dict(msg)
            if to_user:
                rewritten["role"] = "user"
            content = msg.get("content")
            if isinstance(content, list):
                first = content[0] if content else None
                already = isinstance(first, dict) and first.get("text") == marker
                rewritten["content"] = (
                    content if already else [{"type": "text", "text": marker}, *content]
                )
            else:
                text = content or ""
                rewritten["content"] = (
                    text if str(text).startswith(marker) else f"{ENGINE_CONTEXT_PREFIX}{text}"
                )
            normalized.append(rewritten)

        logger.debug(
            "Normalized %d developer-role message(s) to user turns for %s",
            converted,
            model,
        )
        return normalized

    @staticmethod
    def _defang_user_marker(msg: dict[str, Any], marker: str) -> dict[str, Any]:
        """A user turn never starts a line with the engine marker.

        ``[engine]`` at the start of a line becomes ``[not engine]``; anything
        else in the message is untouched, and a message without the marker is
        returned as the same object.
        """
        import re

        pattern = re.compile(r"(^|\n)[ \t]*" + re.escape(marker))

        def _clean(text: str) -> str:
            return pattern.sub(lambda m: f"{m.group(1)}[not engine]", text)

        content = msg.get("content")
        if isinstance(content, str):
            cleaned = _clean(content)
            if cleaned == content:
                return msg
            return {**msg, "content": cleaned}
        if isinstance(content, list):
            changed = False
            blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    cleaned = _clean(block["text"])
                    if cleaned != block["text"]:
                        changed = True
                        block = {**block, "text": cleaned}
                blocks.append(block)
            return {**msg, "content": blocks} if changed else msg
        return msg

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
        thinking_reduced: bool = False,
        nudge: str | None = None,
    ) -> dict[str, Any]:
        """Build kwargs dict for litellm.acompletion.

        ``request_timeout`` overrides the HTTP-level timeout so it stays in
        step with the caller's per-call ``asyncio.timeout`` budget (e.g. the
        batch timeout for cron/workflow runs).

        ``thinking_reduced`` and ``nudge`` are the re-ask after a reasoning-only
        reply (DIAG 2026-09-13 §4.1): a smaller thinking budget and one extra
        user turn asking for the answer, because an identical re-roll truncates
        identically.
        """
        from robothor.engine.model_registry import (
            get_model_limits,
            get_output_tokens,
            supports_cache_control,
        )

        limits = get_model_limits(model)
        actual_model = model

        # Reasoning belongs to the model that produced it: echoed back to the
        # same model (thinking mode rejects a history without it), stripped for
        # any other. Runs first so every later step sees the payload shape the
        # provider will. No-op (same list object) for a history with no
        # reasoning on it. See reasoning_replay.
        messages = strip_reasoning_for_model(messages, model)

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

        if nudge:
            # Appended after every hygiene pass so it cannot be hoisted out of
            # the list or stripped as engine context.
            messages = [*messages, {"role": "user", "content": nudge}]

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

        # Local models: state residency explicitly instead of inheriting the
        # server's defaults. Until 2026-08-27 the engine sent neither, so the
        # fleet's offline tier sat at its full 262144-token context (18.9GB of
        # a shared pool) and expired on the server's 10m default — shorter than
        # several agents' cron cadence, so every cron paid a ~34s cold load.
        #
        # num_ctx comes from the REGISTRY, not a separate knob: the proactive
        # compaction threshold is derived from max_input_tokens, so a window
        # smaller than the registry believes means the engine fills context
        # past what the server allocated and the server truncates in silence.
        # One number, two consumers. test_local_tier_residency pins it.
        if is_local_model(model):
            from robothor.config import get_config

            kwargs["num_ctx"] = limits.max_input_tokens
            try:
                kwargs["keep_alive"] = get_config().ollama.keep_alive_engine
            except Exception as e:  # noqa: BLE001 - residency is an optimisation
                logger.debug("keep_alive unavailable, using server default: %s", e)
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
            kwargs["tool_choice"] = tool_choice(tools)
        kwargs.update(
            thinking_kwargs_for_call(model, kwargs["max_tokens"], reduced=thinking_reduced)
        )
        from robothor.engine.provider_routing import apply_provider_order

        apply_provider_order(model, kwargs)
        from robothor.engine.response_schema import response_format

        schema_format = response_format()
        if schema_format is not None:
            kwargs["response_format"] = schema_format
        elif _response_format_var.get() == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["messages"] = [
                {
                    "role": "system",
                    "content": "Return a single JSON object as the final answer. Do not prepend or append commentary or Markdown fences. Tool calls remain available when needed.",
                },
                *kwargs["messages"],
            ]
        return kwargs

    # ─── Model error handling ────────────────────────────────────────

    @staticmethod
    def _handle_model_error(
        e: Exception,
        model: str,
        broken_models: set[str] | None,
        *,
        streaming: bool = False,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Handle model failure: mark broken or log warning.

        ``messages`` is the history the call carried, used only to describe the
        shape of a rejected conversation (redacted) when the failure is a
        thinking-mode replay rejection.
        """
        # Named before anything else classifies it — a 400 tells the operator
        # nothing on its own.
        _log_reasoning_replay_rejection(model, e, messages)
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

    def _note_malformed_tool_call(self, e: Exception, model: str, *, reroll: bool) -> None:
        """Log an unparseable tool call, and never tell the breaker about it.

        The failure arrives as an `APIConnectionError` (status 500) raised from
        inside litellm's request transformation, so by status alone it is
        indistinguishable from the provider being down — and was treated as
        such: five attempts at a deterministic parse failure, then a breaker
        failure recorded against the on-device tier every agent's chain ends
        in. Bad output is a fact about one completion, not about a provider's
        health, so the chain advances and the breaker hears nothing.
        """
        if reroll:
            logger.warning(
                "Model %s returned unparseable tool-call arguments — "
                "re-rolling the same model once (%s)",
                _sanitize(model),
                _sanitize(e),
            )
        else:
            logger.warning(
                "Model %s keeps returning unparseable tool-call arguments — falling "
                "through to the next model. This is bad output, not a provider "
                "outage; the breaker is not told.",
                _sanitize(model),
            )
        if self._active_watchdog:
            self._active_watchdog.touch(
                f"model_retry:{model}" if reroll else f"model_fallback:{model}"
            )

    async def _wait_out_rate_limit(
        self, e: Exception, model: str, attempt: int, attempts: int
    ) -> bool:
        """Sleep off a 429 if there is an attempt left. True means retry.

        A rate limit is the provider saying "not right now", so the answer is
        the interval it named and the SAME model — not the next one, which is
        usually on the same key and will say the same thing.
        """
        wait = rate_limit_wait_seconds(e)
        if wait is None or attempt >= attempts - 1:
            return False
        logger.warning(
            "Model %s rate-limited — waiting %.1fs and retrying the same model (attempt %d/%d)",
            _sanitize(model),
            wait,
            attempt + 1,
            attempts,
        )
        if self._active_watchdog:
            self._active_watchdog.touch(f"rate_limit_wait:{model}")
        await asyncio.sleep(wait)
        return True

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

        Holds one local inference slot for the call. The gate belongs here rather
        than in ``_call_llm``: every local model reaches litellm through this
        method (codex is never local), and the image retry then stays inside the
        slot it already holds instead of queueing for a second one.

        The refusal is also the one moment the platform ever learns, from a real
        provider, that a model cannot see. It is recorded on the registry
        (:func:`robothor.engine.model_registry.note_image_rejection`) so
        ``view_image`` stops handing this model blocks it will only have to
        strip again — an agent being told it looked and then shown a note where
        the picture was is the defect this whole path exists to end.
        """
        from robothor.engine import model_registry

        model_registry.note_active_model(model)
        async with _local_slot(model):
            try:
                return await bounded_completion(litellm.acompletion, **kwargs)
            except Exception as e:
                if not is_image_unsupported_error(e):
                    raise
                model_registry.note_image_rejection(model)
                stripped, changed = strip_image_blocks(messages)
                if not changed:
                    raise
                logger.warning(
                    "Model %s cannot accept images — retrying without them; "
                    "the agent is told to inspect the file programmatically",
                    _sanitize(model),
                )
                return await bounded_completion(
                    litellm.acompletion, **{**kwargs, "messages": stripped}
                )

    # ─── Non-streaming call ──────────────────────────────────────────

    def _admit(
        self,
        model: str,
        dead_credentials: set[str],
        breaker: Any,
        *,
        ignore_breaker: bool,
    ) -> tuple[str | None, KeyPool | None, str | None]:
        """May this model be dialled? Returns ``(skip reason, pool, env var)``.

        The chain walk's admission step, extracted so the walk reads as one
        decision per line. ``model_var`` comes back because the spent-credential
        branch below needs the same name, and re-deriving it there is how two
        answers to "whose key is this" get to disagree.
        """
        model_var = env_var_for_model(model)
        skip, pool = _skip_model_reason(
            model,
            model_var,
            dead_credentials,
            breaker,
            self._key_pool,
            ignore_breaker=ignore_breaker,
        )
        if skip:
            logger.info("skipping %s — %s", _sanitize(model), skip)
        return skip, pool, model_var

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
        timeout_override: float | None = None,
        ignore_breaker: bool = False,
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
        _warn_if_chain_outgrows_its_budget(models)
        # Credentials proven spent during THIS call. A model whose key is
        # in here cannot succeed, so it is skipped rather than tried.
        dead_credentials: set[str] = set()

        for position, model in enumerate(models):
            if broken_models and model in broken_models:
                continue
            skip, pool, model_var = self._admit(
                model, dead_credentials, breaker, ignore_breaker=ignore_breaker
            )
            if skip:
                continue
            per_call_timeout = _per_call_timeout(model, timeout_override)
            # Rotating through spare credentials must not eat the transient
            # retry budget: a key swap and a flaky provider are different
            # failures. They are counted separately so that configuring a
            # spare cannot buy extra retries against an unrelated 5xx.
            attempts = 1 + (
                LOCAL_CAPACITY_RETRIES if is_local_model(model) else TRANSIENT_RETRIES_PER_MODEL
            )
            # Every attempt on this model — retries and the reasoning-only
            # re-ask included — shares ONE allowance. Each getting its own is
            # what made a batch-trigger dispatch worth an hour of wall clock.
            model_deadline = time.monotonic() + per_call_timeout
            rotations_left = (len(pool) - 1) if pool is not None else 0
            overflow_shrinks_left = CONTEXT_OVERFLOW_SHRINKS
            malformed_retries_left = MALFORMED_TOOL_ARGS_RETRIES
            re_asks_left = REASONING_ONLY_RE_ASKS_PER_MODEL
            # Set only for the re-ask after a reasoning-only reply.
            thinking_reduced = False
            nudge: str | None = None
            attempt = 0
            while attempt < attempts:
                # Inside a workflow step, no call may be given more time than
                # the WORKFLOW has left, and one whose budget is gone does not
                # start at all — which is how one classify step stopped eating
                # a whole 900s email-pipeline run (2026-09-13). INSIDE the loop
                # so an in-place retry is bounded too, and raising outside the
                # `try` so the deadline cannot buy itself a retry. Inert
                # outside a workflow.
                per_call_timeout = bound_call_timeout(per_call_timeout, model)
                # …and no more than what is left of THIS model's own allowance,
                # which all of its attempts share — the in-place retry and the
                # reasoning-only re-ask included. The two clamps compose in this
                # order: a workflow's remaining budget can only ever lower the
                # allowance, never raise it, and neither can hand an attempt
                # time the other has already spent.
                attempt_timeout = _attempt_timeout(model_deadline, per_call_timeout)
                if attempt_timeout < _attempt_floor(per_call_timeout):
                    logger.warning(
                        "Model %s: its per-model time allowance is spent — advancing",
                        _sanitize(model),
                    )
                    break
                attempt_key = None
                attempt_started = time.monotonic()
                noted = False
                try:
                    kwargs = self._build_llm_kwargs(
                        model,
                        messages,
                        tools,
                        input_est,
                        temperature,
                        request_timeout=attempt_timeout,
                        thinking_reduced=thinking_reduced,
                        nudge=nudge,
                    )
                    attempt_key = _bind_attempt_key(pool, kwargs)
                    with self._watchdog_wait(f"llm_inflight:{model}", attempt_timeout):
                        async with asyncio.timeout(attempt_timeout):
                            if is_codex_model(model):
                                result = await bounded_completion(codex_acompletion, **kwargs)
                            else:
                                result = await self._call_with_image_fallback(
                                    model=model,
                                    messages=kwargs.get("messages", []),
                                    kwargs=kwargs,
                                )
                    shape = describe_completion(result)
                    # Decided BEFORE the row is written: a reasoning-only reply
                    # the same model is about to be re-asked about self-healed,
                    # and only the row can tell that from one that gave up.
                    re_ask = shape.reasoning_only and re_asks_left > 0
                    note_outcome(model, attempt_started, shape=shape, self_healed=re_ask)
                    noted = True
                    if shape.no_answer:
                        if re_ask:
                            re_asks_left -= 1
                            thinking_reduced = True
                            nudge = REASONING_ONLY_NUDGE
                            _log_reasoning_only(model, shape)
                            # Deliberately does not advance `attempt`: this is a
                            # different request, not a re-roll of a flaky one.
                            continue
                        # Not a finished answer. Raising routes this into the
                        # transient-retry path below rather than returning a
                        # run that silently produced nothing.
                        raise EmptyCompletionError(
                            f"{model} returned no content and no tool call ({shape.describe()})"
                        )
                    breaker.record_success(model)
                    _record_execution_mode(model)
                    return result
                except asyncio.CancelledError as ce:
                    # A run-deadline cancel, a stall kill or an operator — not
                    # a provider failure, and not an Exception, so the handler
                    # below never saw it. The attempt still happened and still
                    # cost the wall clock this row exists to account for.
                    if not noted:
                        note_outcome(model, attempt_started, error=ce)
                    raise
                except RequestRouteUnavailableError as exc:
                    # No provider request was admitted. Shared workers may have
                    # excluded this model's last endpoint; do not blame its
                    # breaker or bypass the next model's own quote/reservation.
                    last_error = exc
                    logger.info("No eligible funded route for %s — advancing", _sanitize(model))
                    break
                except RequestBudgetError:
                    raise
                except Exception as e:
                    last_error = e
                    # `noted` is already True when the response itself was the
                    # failure (an empty), so it is never counted twice.
                    if not noted:
                        note_outcome(model, attempt_started, error=e)
                    is_timeout = isinstance(e, TimeoutError)
                    # A spent budget is not a busy provider: no wait fixes it,
                    # and every other model on the same key fails identically.
                    # Walking the chain just burns them all.
                    spent = is_credit_exhausted(e)
                    if _rotated_for(
                        e, model, pool, attempt_key, spent=spent, rotations_left=rotations_left
                    ):
                        rotations_left -= 1
                        # Deliberately does not advance `attempt`: a key swap
                        # is not a retry of a flaky provider.
                        continue
                    if spent:
                        if not _spent_credit_leaves_someone_reachable(
                            models, position, model, model_var, dead_credentials
                        ):
                            raise
                        break
                    if is_context_overflow(e):
                        if overflow_shrinks_left > 0 and shrink_after_overflow(messages, model):
                            overflow_shrinks_left -= 1
                            input_est = estimate_tokens(messages)
                            continue
                        logger.warning(
                            "Model %s cannot hold this conversation (%s) — advancing",
                            _sanitize(model),
                            _sanitize(e),
                        )
                        break
                    if is_malformed_tool_arguments(e):
                        if malformed_retries_left > 0:
                            malformed_retries_left -= 1
                            # Advances `attempt`, unlike the key rotation above:
                            # a re-roll is a real call and spends the budget.
                            attempt += 1
                            self._note_malformed_tool_call(e, model, reroll=True)
                            continue
                        # Skips `_handle_model_error`, so a chain naming this
                        # model twice re-rolls it once more. Bounded, and cheap.
                        self._note_malformed_tool_call(e, model, reroll=False)
                        break
                    if await self._wait_out_rate_limit(e, model, attempt, attempts):
                        attempt += 1
                        continue
                    delay = (
                        _retry_delay(e, model, model_deadline, per_call_timeout)
                        if attempt < attempts - 1
                        else None
                    )
                    if delay is not None:
                        logger.warning(
                            "Model %s transient failure (%s) — retrying same model "
                            "in %.1fs (attempt %d/%d)",
                            _sanitize(model),
                            _sanitize(f"timeout after {attempt_timeout}s" if is_timeout else e),
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
                    if _advance_without_blaming_the_model(e, model):
                        break
                    _blame_model(breaker, e, model, attempt_timeout)
                    self._handle_model_error(e, model, broken_models, messages=messages)
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
            # Bounds the initial stream-creation await; subsequent chunk reads
            # are guarded by STREAM_CHUNK_TIMEOUT in the consumption loop below.
            per_call_timeout = _per_call_timeout(model, timeout_override)
            pool = self._key_pool(model)
            skip = _streaming_skip_reason(model, pool)
            if skip:
                logger.info("skipping %s — %s", _sanitize(model), skip)
                continue
            rotations_left = (len(pool) - 1) if pool is not None else 0
            overflow_shrinks_left = CONTEXT_OVERFLOW_SHRINKS
            while True:
                # INSIDE the loop, as in _call_llm: a rotation retry re-enters
                # here and would otherwise reuse a stale, possibly elapsed
                # timeout. After the skips, so it names only a dialled model.
                per_call_timeout = bound_call_timeout(per_call_timeout, model)
                attempt_key = None
                attempt_started = time.monotonic()
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
                    attempt_key = _bind_attempt_key(pool, kwargs)
                    if is_codex_model(model):
                        with self._watchdog_wait(f"llm_inflight:{model}", per_call_timeout):
                            async with asyncio.timeout(per_call_timeout):
                                result = await bounded_completion(codex_acompletion, **kwargs)
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
                        note_outcome(model, attempt_started, shape=describe_completion(result))
                        return result

                    stream_start = time.monotonic()
                    with self._watchdog_wait(f"llm_inflight:{model}", per_call_timeout):
                        async with asyncio.timeout(per_call_timeout):
                            stream = await _gated_acompletion(model, kwargs)

                    chunks: list[Any] = []
                    accumulated_content = ""
                    has_tool_calls = False
                    ttft_logged = False
                    seen_tool_ids: set[str] = set()
                    # stream_chunk_builder combines reasoning_content and drops
                    # the rest, so this one is accumulated here or it is lost —
                    # and the interactive path would replay fewer fields than a
                    # cron run of the same agent. See reasoning_replay.
                    streamed_reasoning_details: list[Any] = []

                    # Consume stream with per-chunk timeout so stalled streams
                    # fall back to the next model instead of hanging the run.
                    chunk_iter = stream.__aiter__()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                chunk_iter.__anext__(),
                                timeout=stream_chunk_timeout(model),
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
                            await _emit_usage(chunk, _emit)
                            continue
                        delta = chunk.choices[0].delta
                        delta_details = getattr(delta, "reasoning_details", None)
                        if isinstance(delta_details, list):
                            streamed_reasoning_details.extend(delta_details)
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
                            await _emit_tool_call_events(delta.tool_calls, seen_tool_ids, _emit)

                    await _emit({"type": "message_stop"})
                    # Final progress tick — we have a complete response to return.
                    if self._active_watchdog:
                        self._active_watchdog.touch(f"stream_complete:{model}")
                    rebuilt = litellm.stream_chunk_builder(chunks)
                    merge_streamed_reasoning_details(rebuilt, streamed_reasoning_details)
                    # One meaning for `duration_ms` on both paths: the provider
                    # attempt, never the token-counting prep before it.
                    shape = _streamed_shape(rebuilt, accumulated_content, has_tool_calls)
                    note_outcome(model, attempt_started, shape=shape)
                    if shape.no_answer:
                        last_error = _streamed_without_an_answer(model, shape)
                        break
                    # The interactive path proves a model healthy more often
                    # than any cron does; without this only failures ever
                    # reach the breaker, so it can open and never clear.
                    get_model_breaker().record_success(model)
                    _record_execution_mode(model)
                    return rebuilt
                except RequestRouteUnavailableError as exc:
                    last_error = exc
                    logger.info("No eligible funded route for %s — advancing", _sanitize(model))
                    break
                except RequestBudgetError:
                    raise
                except TimeoutError as te:
                    note_outcome(model, attempt_started, error=te)
                    self._handle_model_error(te, model, broken_models, streaming=True)
                    last_error = te
                    # Model rotation is activity — don't let watchdog kill us mid-fallback
                    if self._active_watchdog:
                        self._active_watchdog.touch(f"stream_timeout_fallback:{model}")
                    break
                except Exception as e:
                    note_outcome(model, attempt_started, error=e)
                    if _rotated_for(
                        e,
                        model,
                        pool,
                        attempt_key,
                        spent=is_credit_exhausted(e),
                        rotations_left=rotations_left,
                    ):
                        rotations_left -= 1
                        continue
                    if is_context_overflow(e):
                        # Safe to re-ask in place for the same reason the
                        # credential rotation above is: an oversized
                        # conversation is refused at stream CREATION, before
                        # any chunk has reached `on_content`, so nothing the
                        # user has already seen can be duplicated. Every
                        # signature in `OVERFLOW_SIGNATURES` is a request-time
                        # failure; one that arrived mid-stream would need a
                        # "nothing emitted yet" guard instead.
                        if overflow_shrinks_left > 0 and shrink_after_overflow(messages, model):
                            overflow_shrinks_left -= 1
                            input_est = estimate_tokens(messages)
                            continue
                        last_error = e
                        break
                    if _advance_without_blaming_the_model(e, model):
                        last_error = e
                        break
                    self._handle_model_error(
                        e, model, broken_models, streaming=True, messages=messages
                    )
                    last_error = e
                    if self._active_watchdog:
                        self._active_watchdog.touch(f"stream_error_fallback:{model}")
                    break

        # repr(): str(TimeoutError()) is "" — see _call_llm's exhaustion log.
        logger.error("All models failed (streaming). Last error: %s", _sanitize(repr(last_error)))
        return None

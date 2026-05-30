"""Shared LLM call abstraction for the Agent Engine.

Provides three entry points that wrap ``litellm.acompletion`` with consistent
timeout handling, retry logic, and multi-model fallback:

- :func:`llm_call` — single-model call with optional retry.
- :func:`llm_call_with_fallback` — multi-model fallback (non-streaming).
- :func:`llm_call_streaming` — multi-model fallback with streaming.

This module is Phase 3A of the enterprise-hardening effort.  Callers
(planner, verifier, compaction, PDF handler) will be migrated in Phase 3B.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import time as _time
from collections.abc import Awaitable, Callable  # noqa: TC003
from typing import TYPE_CHECKING, Any

import litellm

from robothor.engine.codex_provider import CodexProviderError, is_codex_model
from robothor.engine.codex_provider import acompletion as codex_acompletion
from robothor.engine.metrics import LLM_CALL_DURATION, LLM_CALLS_TOTAL, LLM_TOKENS_TOTAL
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
LLM_REQUEST_TIMEOUT = 120
LLM_REQUEST_TIMEOUT_OLLAMA = 600


class AllModelsFailedError(Exception):
    """All models in a fallback chain failed."""


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


async def llm_call_with_fallback(
    messages: list[dict[str, Any]],
    *,
    models: list[str],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.3,
    timeout_budget: int | float = 180,
    max_tokens: int | None = None,
) -> Any:
    """Multi-model fallback LLM call (non-streaming).

    Iterates through *models* in order, moving to the next on failure.

    Args:
        messages: Chat messages in OpenAI format.
        models: Ordered list of model identifiers to try.
        tools: Optional tool definitions (OpenAI function-calling format).
        temperature: Sampling temperature.
        timeout_budget: Total wall-clock seconds shared across all models.
        max_tokens: Optional max output tokens.

    Returns:
        The ``litellm.ModelResponse``.

    Raises:
        AllModelsFailedError: When every model in the chain has failed.
    """
    if not models:
        raise AllModelsFailedError("No models provided")

    per_model_timeout = max(30, int(timeout_budget) // len(models))
    last_error: Exception | None = None

    for model in models:
        t0 = _time.monotonic()
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if tools:
                kwargs["tools"] = tools
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens

            call = codex_acompletion if is_codex_model(model) else litellm.acompletion
            resp = await asyncio.wait_for(call(**kwargs), timeout=per_model_timeout)
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
        except TimeoutError:
            LLM_CALLS_TOTAL.labels(model=model, status="error").inc()
            LLM_CALL_DURATION.labels(model=model).observe(_time.monotonic() - t0)
            logger.warning("Model %s timed out after %ds, trying next", model, per_model_timeout)
            last_error = TimeoutError(f"Model {model} timed out after {per_model_timeout}s")
        except Exception as e:
            LLM_CALLS_TOTAL.labels(model=model, status="error").inc()
            LLM_CALL_DURATION.labels(model=model).observe(_time.monotonic() - t0)
            logger.warning("Model %s failed: %s, trying next", model, e)
            last_error = e

    raise AllModelsFailedError(f"All models failed. Last error: {last_error}") from last_error


async def llm_call_streaming(
    messages: list[dict[str, Any]],
    *,
    models: list[str],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.3,
    timeout_budget: int | float = 180,
    max_tokens: int | None = None,
    on_chunk: Callable[[Any], Awaitable[None]] | None = None,
) -> list[Any]:
    """Streaming multi-model fallback LLM call.

    Same fallback semantics as :func:`llm_call_with_fallback`, but requests
    ``stream=True`` and optionally invokes *on_chunk* for each chunk.

    Returns a list of all received chunks (for the caller to reconstruct the
    full response).

    Args:
        messages: Chat messages in OpenAI format.
        models: Ordered list of model identifiers to try.
        tools: Optional tool definitions (OpenAI function-calling format).
        temperature: Sampling temperature.
        timeout_budget: Total wall-clock seconds shared across all models.
        max_tokens: Optional max output tokens.
        on_chunk: Optional async callback invoked with each stream chunk.

    Returns:
        List of stream chunks.

    Raises:
        AllModelsFailedError: When every model in the chain has failed.
    """
    if not models:
        raise AllModelsFailedError("No models provided")

    per_model_timeout = max(30, int(timeout_budget) // len(models))
    last_error: Exception | None = None

    for model in models:
        t0 = _time.monotonic()
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens

            async def _consume_stream(
                _kw: dict[str, Any] = kwargs,
                _model: str = model,
            ) -> list[Any]:
                if is_codex_model(_model):
                    resp = await codex_acompletion(**_kw)
                    if on_chunk is not None:
                        await on_chunk(resp)
                    return [resp]
                s = await litellm.acompletion(**_kw)
                collected: list[Any] = []
                async for chunk in s:
                    collected.append(chunk)
                    if on_chunk is not None:
                        await on_chunk(chunk)
                return collected

            chunks = await asyncio.wait_for(_consume_stream(), timeout=per_model_timeout)
            LLM_CALLS_TOTAL.labels(model=model, status="success").inc()
            LLM_CALL_DURATION.labels(model=model).observe(_time.monotonic() - t0)
            return chunks
        except TimeoutError:
            LLM_CALLS_TOTAL.labels(model=model, status="error").inc()
            LLM_CALL_DURATION.labels(model=model).observe(_time.monotonic() - t0)
            logger.warning(
                "Model %s timed out after %ds (streaming), trying next",
                model,
                per_model_timeout,
            )
            last_error = TimeoutError(f"Model {model} timed out after {per_model_timeout}s")
        except Exception as e:
            LLM_CALLS_TOTAL.labels(model=model, status="error").inc()
            LLM_CALL_DURATION.labels(model=model).observe(_time.monotonic() - t0)
            logger.warning("Model %s failed (streaming): %s, trying next", model, e)
            last_error = e

    raise AllModelsFailedError(
        f"All models failed (streaming). Last error: {last_error}"
    ) from last_error


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
        """
        if is_codex_model(model_used):
            return self._calculate_cost(
                model_used,
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
            )
        try:
            cost = litellm.completion_cost(completion_response=response, model=models[0])
            if cost and cost > 0:
                return cost
        except Exception as e:
            logger.warning("litellm cost calculation failed, using fallback: %s", e)
        return self._calculate_cost(
            models[0],
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
        if on_content or on_stream_event:
            return await self._call_llm_streaming(
                session.messages,
                models,
                tool_schemas,
                on_content,
                broken_models=broken_models,
                temperature=temperature,
                on_stream_event=on_stream_event,
            )
        return await self._call_llm(
            session.messages,
            models,
            tool_schemas,
            broken_models=broken_models,
            temperature=temperature,
        )

    # ─── Pre-flight ──────────────────────────────────────────────────

    async def _prepare_llm_call(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
    ) -> int:
        """Shared pre-flight: compress context and estimate input tokens.

        Mutates messages in-place. Returns estimated input token count.
        """
        from robothor.engine.context import estimate_tokens, maybe_compress
        from robothor.engine.model_registry import get_model_limits

        try:
            model_limits = get_model_limits(models[0])
            compress_threshold = int(model_limits.max_input_tokens * 0.75)
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
    def _guard_trailing_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop a trailing assistant message before an LLM call.

        OpenRouter-proxied Anthropic (via Azure/Google) rejects requests whose
        final message is an assistant turn with:
            "This model does not support assistant message prefill.
             The conversation must end with a user message."

        A trailing assistant normally indicates an orphaned turn — e.g. a prior
        run's response that was carried into history without its follow-up, or
        a checkpoint restored after a user turn was lost. Drop it so the LLM
        call can proceed.
        """
        if messages and messages[-1].get("role") == "assistant":
            logger.warning(
                "Dropping trailing assistant message before LLM call (prefill-rejection guard)"
            )
            return messages[:-1]
        return messages

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
    ) -> dict[str, Any]:
        """Build kwargs dict for litellm.acompletion."""
        from robothor.engine.model_registry import get_model_limits, get_output_tokens

        limits = get_model_limits(model)
        actual_model = model

        # For direct Anthropic API models, enable prompt caching on the system
        # message by converting it to content-block format with cache_control.
        # OpenRouter models (e.g. "openrouter/anthropic/claude-sonnet-4-6") must
        # NOT get this conversion — litellm sends them via the OpenAI-compatible
        # path and the mixed format causes tool_use/tool_result pairing failures.
        is_direct_anthropic = model.startswith("anthropic/")
        if is_direct_anthropic and messages and messages[0].get("role") == "system":
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

        # Defense in depth: drop a trailing assistant message, which OpenRouter-
        # proxied Anthropic rejects with "model does not support prefill".
        messages = LLMClient._guard_trailing_assistant(messages)

        kwargs: dict[str, Any] = {
            "model": actual_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": get_output_tokens(model, input_est),
            "timeout": (
                LLM_REQUEST_TIMEOUT_OLLAMA
                if model.startswith("ollama_chat/")
                else LLM_REQUEST_TIMEOUT
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

    # ─── Non-streaming call ──────────────────────────────────────────

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
    ) -> Any:
        """Call LLM with model fallback. Returns litellm response or None."""
        input_est = await self._prepare_llm_call(messages, models)
        last_error: Exception | None = None

        logger.debug(
            "LLM call with models: %s (broken: %s)",
            _sanitize(models),
            _sanitize(broken_models or set()),
        )
        for model in models:
            if broken_models and model in broken_models:
                continue
            # Per-call timeout (seconds) — wraps each provider call so the
            # runner cancels and falls through if the provider hangs. The
            # `timeout` kwarg already passed to litellm is best-effort and
            # was observed silently ignored, causing 1800s stalls against
            # codex/gpt-5.5 in the 2026-05-28 incident.
            per_call_timeout = (
                LLM_REQUEST_TIMEOUT_OLLAMA
                if model.startswith("ollama_chat/")
                else LLM_REQUEST_TIMEOUT
            )
            try:
                kwargs = self._build_llm_kwargs(model, messages, tools, input_est, temperature)
                async with asyncio.timeout(per_call_timeout):
                    if is_codex_model(model):
                        result = await codex_acompletion(**kwargs)
                    else:
                        result = await litellm.acompletion(**kwargs)
                return result
            except TimeoutError as e:
                logger.warning(
                    "LLM call to %s exceeded %ds — cancelling and falling back",
                    _sanitize(model),
                    per_call_timeout,
                )
                self._handle_model_error(e, model, broken_models)
                last_error = e
                if self._active_watchdog:
                    self._active_watchdog.touch(f"model_fallback:{model}")
            except Exception as e:
                self._handle_model_error(e, model, broken_models)
                last_error = e
                if self._active_watchdog:
                    self._active_watchdog.touch(f"model_fallback:{model}")

        logger.error(
            "All models failed. Models: %s, broken: %s, last error: %s",
            _sanitize(models),
            _sanitize(broken_models or set()),
            _sanitize(last_error),
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
    ) -> Any:
        """Call LLM with streaming. Returns reconstructed ModelResponse.

        Emits structured events to ``on_stream_event`` if provided:
        - ``{"type": "text_delta", "delta": "...", "accumulated": "..."}``
        - ``{"type": "tool_use_start", "tool_name": "...", "call_id": "..."}``
        - ``{"type": "tool_use_delta", "delta": "...", "call_id": "..."}``
        - ``{"type": "usage", "input_tokens": N, "output_tokens": N}``
        - ``{"type": "message_stop"}``
        """
        input_est = await self._prepare_llm_call(messages, models)
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
            per_call_timeout = (
                LLM_REQUEST_TIMEOUT_OLLAMA
                if model.startswith("ollama_chat/")
                else LLM_REQUEST_TIMEOUT
            )
            try:
                kwargs = self._build_llm_kwargs(
                    model, messages, tools, input_est, temperature, stream=True
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

        logger.error("All models failed (streaming). Last error: %s", last_error)
        return None

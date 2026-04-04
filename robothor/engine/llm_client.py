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
import logging
import time as _time
from collections.abc import Awaitable, Callable  # noqa: TC003
from typing import Any

import litellm

from robothor.engine.metrics import LLM_CALL_DURATION, LLM_CALLS_TOTAL, LLM_TOKENS_TOTAL
from robothor.engine.retry import retry_async

logger = logging.getLogger(__name__)


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
            resp = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=timeout)
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

            resp = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=per_model_timeout)
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

            async def _consume_stream(_kw: dict[str, Any] = kwargs) -> list[Any]:
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

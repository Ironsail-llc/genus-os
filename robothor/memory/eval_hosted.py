"""Model calls for the benchmark runner: local Ollama, or a hosted model.

A model named ``openai/<id>`` is hosted and goes through litellm, the engine's
own client (``robothor.engine.llm_client`` uses the same call and its helpers
decide what a reasoning model accepts). Everything else is a local Ollama
model. Nothing falls back: a hosted call that fails raises, so a run is never
labelled with one model and answered by another.

The key comes from the shared credential pool
(``robothor.engine.key_pool.api_key_for_model``: ``OPENAI_API_KEY``, then
``OPENAI_API_KEY_2`` …), as every other provider call takes it; a key the
provider rejects (401, 402, 403) is retired there, so the next call rotates.
With no pooled key litellm resolves the environment itself. The endpoint
comes from ``OPENAI_API_BASE`` (or ``OPENAI_BASE_URL``) when set, since a key
can be bound to a regional endpoint. Neither appears in a log, a report or a
cache.

Prices are the provider's list prices in US dollars per million tokens, as
stated on 2026-11-26; check them before a run. A dry run multiplies them by
the estimated tokens; a real run multiplies them by the token counts the
provider reports for each call.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

HOSTED_PREFIX = "openai/"
#: Hosted calls in flight at once, whatever the run's concurrency.
MAX_IN_FLIGHT = 4
_MAX_ATTEMPTS = 8
_BACKOFF_BASE_S = 2.0
_BACKOFF_CAP_S = 60.0
#: Room a reasoning model needs beyond its answer: reasoning tokens count
#: against the completion ceiling, which is billed only for what is used.
REASONING_HEADROOM = 8192

#: USD per 1M tokens (input, output), by model id without the provider prefix.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-5-mini": (0.25, 2.00),
}

_gate: asyncio.Semaphore | None = None
_gate_loop: asyncio.AbstractEventLoop | None = None


def is_hosted(model: str) -> bool:
    return model.startswith(HOSTED_PREFIX)


def bare(model: str) -> str:
    return model.removeprefix(HOSTED_PREFIX)


def price(model: str) -> tuple[float, float]:
    try:
        return PRICES_USD_PER_MTOK[bare(model)]
    except KeyError:
        raise ValueError(
            f"no price for {model!r}; known: {', '.join(sorted(PRICES_USD_PER_MTOK))}"
        ) from None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price(model)
    return input_tokens * pin / 1e6 + output_tokens * pout / 1e6


def is_reasoning(model: str) -> bool:
    """A hosted model that reasons (GPT-5 family, o-series), as the engine decides it."""
    if not is_hosted(model):
        return False
    try:
        from robothor.engine.llm_client import is_openai_reasoning_model

        return bool(is_openai_reasoning_model(model))
    except Exception:  # noqa: BLE001 - the engine's catalog may be unavailable
        name = bare(model)
        return name.startswith(("gpt-5", "o1", "o3", "o4"))


def _gate_for_loop() -> asyncio.Semaphore:
    global _gate, _gate_loop
    loop = asyncio.get_running_loop()
    if _gate is None or _gate_loop is not loop:
        _gate, _gate_loop = asyncio.Semaphore(MAX_IN_FLIGHT), loop
    return _gate


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _is_rate_limit(exc: Exception) -> bool:
    import litellm

    if isinstance(exc, litellm.exceptions.RateLimitError):
        return True
    status = getattr(exc, "status_code", None)
    return status in (429, 500, 502, 503, 504)


#: Provider statuses that mean this credential is spent or refused.
_REJECTED = (401, 402, 403)


def _retire(model: str, key: str, exc: Exception) -> None:
    """Take a rejected key out of rotation, classified as the engine does."""
    from robothor.engine.key_pool import Retirement, retire_for_model

    detail = str(exc).lower()
    if any(m in detail for m in ("weekly limit", "daily limit", "monthly limit")):
        reason = Retirement.QUOTA_EXHAUSTED_PERIODIC
    elif getattr(exc, "status_code", None) == 401:
        reason = Retirement.AUTH_FAILED
    else:
        reason = Retirement.CREDIT_EXHAUSTED
    retire_for_model(model, key, reason)


async def hosted_chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 512,
    think: bool = False,
    **_ignored: Any,
) -> Any:
    """One hosted chat completion; returns ``eval_bench.ChatResult``.

    A reasoning model takes no sampling temperature and gets
    ``REASONING_HEADROOM`` on top of ``max_tokens`` (``think`` is a local
    switch and is ignored: a hosted model reasons as its provider runs it).
    Rate limits and transient server errors back off exponentially; anything
    else raises.
    """
    import litellm

    from robothor.memory.eval_bench import ChatResult

    kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if is_reasoning(model):
        kwargs["max_tokens"] = max_tokens + REASONING_HEADROOM
    else:
        kwargs["temperature"] = temperature
    from robothor.engine.key_pool import api_key_for_model
    from robothor.settings.env import process_env_get

    base = process_env_get("OPENAI_API_BASE", None) or process_env_get("OPENAI_BASE_URL", None)
    if base:
        kwargs["api_base"] = base
    key = api_key_for_model(model)
    if key is not None:
        kwargs["api_key"] = key
    gate = _gate_for_loop()
    attempt = 0
    while True:
        try:
            async with gate:
                response = await litellm.acompletion(**kwargs)
            break
        except Exception as exc:
            attempt += 1
            if key is not None and getattr(exc, "status_code", None) in _REJECTED:
                _retire(model, key, exc)
                raise
            if attempt >= _MAX_ATTEMPTS or not _is_rate_limit(exc):
                raise
            await _sleep(min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (attempt - 1)))
    choice = response.choices[0]
    text = str(getattr(choice.message, "content", "") or "")
    usage = getattr(response, "usage", None)
    tin = int(getattr(usage, "prompt_tokens", 0) or 0)
    tout = int(getattr(usage, "completion_tokens", 0) or 0)
    try:
        spend: float | None = cost_usd(model, tin, tout)
    except ValueError:
        spend = None
    return ChatResult(text, tin, tout, spend)


def chat_for(model: str) -> Callable[..., Awaitable[Any]]:
    """The call for ``model``: hosted for ``openai/…``, local Ollama otherwise."""
    if is_hosted(model):
        return hosted_chat
    from robothor.llm import ollama

    return ollama.chat

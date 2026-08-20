"""Memory generation dispatch — local Ollama by default, optional remote provider.

Every LLM *generation* call the memory system makes (fact extraction, episode
summaries, insight discovery, conflict classification, intent inference,
preference distillation, consolidation) goes through this seam. Embeddings and
reranking stay on local Ollama — they are cheap and fast; generation is the
expensive part that saturates the local GPU (~60s per memory write) and can be
offloaded to a remote provider.

Configuration (read at call time, not import time):

- ``ROBOTHOR_MEMORY_GENERATION_PROVIDER``: ``ollama`` (default — behavior
  unchanged) or ``openrouter``.
- ``ROBOTHOR_MEMORY_GENERATION_REMOTE_MODEL``: remote model id, default
  ``openrouter/xiaomi/mimo-v2.5`` (the litellm-style ``openrouter/`` prefix
  is accepted and stripped for the raw API call).
- ``OPENROUTER_API_KEY``: required when the provider is ``openrouter``.

Failure policy (audit lesson: silent fallbacks hide primary-model death):

- Remote call fails → fall back to local Ollama, log a WARNING containing
  ``MEMORY_GENERATION_REMOTE_FALLBACK`` and increment
  ``remote_fallback_count``. Never silent.
- Provider is ``openrouter`` but ``OPENROUTER_API_KEY`` is unset → log an
  ERROR containing ``MEMORY_GENERATION_REMOTE_MISCONFIGURED`` once per
  process and use local.

Parity notes: memory prompts were written for local qwen3 via Ollama, which
returns reasoning in a separate ``thinking`` field and enforces JSON schemas
via the ``format`` parameter. The remote path maps ``format`` to an
OpenAI-style ``response_format`` json_schema and strips any inline
``<think>…</think>`` blocks, so callers see identical behavior.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import re
from typing import Any

import httpx

from robothor.llm import ollama

logger = logging.getLogger(__name__)

PROVIDER_ENV = "ROBOTHOR_MEMORY_GENERATION_PROVIDER"
REMOTE_MODEL_ENV = "ROBOTHOR_MEMORY_GENERATION_REMOTE_MODEL"
DEFAULT_REMOTE_MODEL = "openrouter/xiaomi/mimo-v2.5"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
REMOTE_TIMEOUT_S = 60.0

# Bounded remote concurrency: the nightly memory batch used to fan out
# unthrottled and got rate-limited 26x in one hour (2026-08-20), silently
# pushing the whole load back onto the local GPU the remote offload exists
# to protect.
CONCURRENCY_ENV = "ROBOTHOR_MEMORY_GENERATION_CONCURRENCY"
DEFAULT_REMOTE_CONCURRENCY = 4

# 429s are retried with exponential backoff BEFORE falling back to local
# ollama — a rate limit is a "slow down" signal, not a provider outage.
RETRY_429_MAX_ATTEMPTS = 4  # 1 initial call + 3 backoff retries
RETRY_429_BASE_SECONDS = 2.0
RETRY_429_CAP_SECONDS = 30.0
RETRY_429_JITTER_FRACTION = 0.25

# After this many CONSECUTIVE remote→local fallbacks, escalate to ERROR with
# a grep-able marker: sustained fallback means the GPU offload is defeated
# and the operator should look at the remote provider.
FALLBACK_STREAK_THRESHOLD = 5
FALLBACK_STREAK_MARKER = "MEMORY_GENERATION_REMOTE_FALLBACK_STREAK"

# Remote reasoning tokens count against max_tokens (unlike local Ollama, which
# budgets the separate ``thinking`` channel on top of num_predict — see
# ollama.chat's thinking_overhead). Mirror that allowance, or small-budget
# callers (judge_importance passes max_tokens=64) get their entire completion
# budget consumed by reasoning and an empty content back (live-confirmed on
# xiaomi/mimo-v2.5: finish_reason=length, content="").
REMOTE_THINKING_OVERHEAD = 8192

# Distinctive log markers — grep targets for alerting.
FALLBACK_MARKER = "MEMORY_GENERATION_REMOTE_FALLBACK"
MISSING_KEY_MARKER = "MEMORY_GENERATION_REMOTE_MISCONFIGURED"

# Module counter: number of remote-generation calls that fell back to local.
remote_fallback_count: int = 0

# CONSECUTIVE fallbacks since the last remote success — resets on success,
# drives the FALLBACK_STREAK_MARKER escalation.
consecutive_fallback_count: int = 0

# Log the missing-key ERROR once per process, not once per memory write.
_missing_key_logged: bool = False

# Per-event-loop semaphore bounding concurrent remote calls. Lazily (re)built
# so CLI scripts with their own asyncio.run() don't trip over a semaphore
# bound to a dead loop.
_remote_sem: asyncio.Semaphore | None = None
_remote_sem_loop: asyncio.AbstractEventLoop | None = None


def _remote_concurrency() -> int:
    """Max concurrent remote generation calls (env-tunable, defaults sane)."""
    raw = os.environ.get(CONCURRENCY_ENV, "").strip()
    if not raw:
        return DEFAULT_REMOTE_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer — using default %d",
            CONCURRENCY_ENV,
            raw,
            DEFAULT_REMOTE_CONCURRENCY,
        )
        return DEFAULT_REMOTE_CONCURRENCY
    return value if value > 0 else DEFAULT_REMOTE_CONCURRENCY


def _remote_semaphore() -> asyncio.Semaphore:
    """The current loop's remote-concurrency semaphore."""
    global _remote_sem, _remote_sem_loop
    loop = asyncio.get_running_loop()
    if _remote_sem is None or _remote_sem_loop is not loop:
        _remote_sem = asyncio.Semaphore(_remote_concurrency())
        _remote_sem_loop = loop
    return _remote_sem


def _retry_429_backoff_seconds(attempt: int, response: httpx.Response | None = None) -> float:
    """Jittered exponential backoff for 429 retry ``attempt`` (0-indexed).

    Honors a finite Retry-After header when present, clamped to the cap so a
    misbehaving server can't blow the retry budget.
    """
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                seconds: float | None = float(retry_after)
            except ValueError:
                seconds = None
            if seconds is not None and math.isfinite(seconds):
                return min(max(seconds, 0.0), RETRY_429_CAP_SECONDS)

    # float() because int ** int is Any to mypy (recorded platform lesson).
    capped = min(RETRY_429_BASE_SECONDS * float(2**attempt), RETRY_429_CAP_SECONDS)
    jitter = capped * RETRY_429_JITTER_FRACTION
    return max(0.0, capped + random.uniform(-jitter, jitter))


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_blocks(text: str) -> str:
    """Remove reasoning ``<think>…</think>`` blocks from remote model output.

    Local Ollama returns reasoning in a separate ``thinking`` field, so
    memory callers never see it. Remote reasoning models (e.g. mimo) may emit
    it inline; strip it so callers get the same clean content either way.
    Also handles an unmatched leading close tag (some chat templates strip
    the opening ``<think>``).
    """
    if "</think>" in text and "<think>" not in text.split("</think>", 1)[0]:
        text = text.split("</think>", 1)[1]
    return _THINK_BLOCK_RE.sub("", text).strip()


def _provider() -> str:
    return os.environ.get(PROVIDER_ENV, "ollama").strip().lower()


def _remote_model() -> str:
    """Remote model id for the raw OpenRouter API (litellm prefix stripped)."""
    model = os.environ.get(REMOTE_MODEL_ENV, DEFAULT_REMOTE_MODEL).strip()
    return model.removeprefix("openrouter/")


def _remote_enabled() -> bool:
    """True when the remote provider is selected and usable."""
    global _missing_key_logged
    if _provider() != "openrouter":
        return False
    if not os.environ.get("OPENROUTER_API_KEY"):
        if not _missing_key_logged:
            logger.error(
                "%s: %s=openrouter but OPENROUTER_API_KEY is not set — "
                "memory generation will use local ollama (logged once)",
                MISSING_KEY_MARKER,
                PROVIDER_ENV,
            )
            _missing_key_logged = True
        return False
    return True


def _record_fallback(error: Exception) -> None:
    global consecutive_fallback_count, remote_fallback_count
    remote_fallback_count += 1
    consecutive_fallback_count += 1
    logger.warning(
        "%s #%d: remote memory generation via %s failed (%s: %s) — falling back to local ollama",
        FALLBACK_MARKER,
        remote_fallback_count,
        _remote_model(),
        type(error).__name__,
        error,
    )
    # Fires at the threshold and every multiple after it, so a sustained
    # outage keeps re-surfacing in the journal instead of alarming once.
    if consecutive_fallback_count % FALLBACK_STREAK_THRESHOLD == 0:
        logger.error(
            "%s: %d consecutive remote memory-generation fallbacks — the GPU "
            "offload is defeated; check the remote provider (%s) and its rate limits",
            FALLBACK_STREAK_MARKER,
            consecutive_fallback_count,
            _remote_model(),
        )


def _record_remote_success() -> None:
    global consecutive_fallback_count
    consecutive_fallback_count = 0


async def _openrouter_chat(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    format: Any | None,  # noqa: A002 — parity with ollama.chat
    think: bool = True,
) -> str:
    """One OpenRouter chat-completions call, normalized to Ollama semantics.

    ``think`` parity: local Ollama gives thinking its own token budget and
    keeps it out of the caller's view. Remotely, reasoning tokens share
    ``max_tokens`` with the answer, so ``think=True`` adds the same overhead
    ollama.chat adds; ``think=False`` disables reasoning outright via
    OpenRouter's ``reasoning`` config (models without reasoning control
    ignore it; a hard 4xx falls back to local like any other remote error).
    """
    payload: dict[str, Any] = {
        "model": _remote_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens + REMOTE_THINKING_OVERHEAD if think else max_tokens,
    }
    if not think:
        payload["reasoning"] = {"enabled": False}
    if isinstance(format, dict):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "memory_generation",
                "strict": True,
                "schema": format,
            },
        }
    elif format == "json":
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
    }
    # Bounded concurrency + 429 backoff: the nightly batch must not stampede
    # the remote provider, and a rate limit is retried (it means "slow down"),
    # not immediately dumped onto the local GPU.
    async with _remote_semaphore():
        async with httpx.AsyncClient(timeout=REMOTE_TIMEOUT_S) as client:
            for attempt in range(RETRY_429_MAX_ATTEMPTS):
                resp = await client.post(OPENROUTER_API_URL, json=payload, headers=headers)
                if resp.status_code == 429 and attempt < RETRY_429_MAX_ATTEMPTS - 1:
                    wait = _retry_429_backoff_seconds(attempt, resp)
                    logger.warning(
                        "remote memory generation rate-limited (429) — retrying "
                        "in %.1fs (attempt %d/%d)",
                        wait,
                        attempt + 1,
                        RETRY_429_MAX_ATTEMPTS,
                    )
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break

    content = strip_think_blocks(data["choices"][0]["message"]["content"] or "")
    if not content:
        raise RuntimeError(f"empty content from remote model {payload['model']}")
    logger.info(
        "remote memory generation: %d content chars via %s",
        len(content),
        payload["model"],
    )
    return content


async def chat(
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 4096,
    model: str | None = None,
    think: bool = True,
    format: Any | None = None,  # noqa: A002 — parity with ollama.chat
) -> str:
    """Chat completion for memory generation. Same contract as ollama.chat.

    ``model`` overrides only apply to the local path (local model names are
    meaningless to the remote provider, whose model comes from
    ``ROBOTHOR_MEMORY_GENERATION_REMOTE_MODEL``).
    """
    if _remote_enabled():
        try:
            content = await _openrouter_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                format=format,
                think=think,
            )
        except Exception as e:
            _record_fallback(e)
        else:
            _record_remote_success()
            return content
    return await ollama.chat(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        think=think,
        format=format,
    )


async def generate(
    prompt: str,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    model: str | None = None,
    think: bool = True,
    format: Any | None = None,  # noqa: A002 — parity with ollama.generate
) -> str:
    """Prompt-style generation for memory. Same contract as ollama.generate."""
    if _remote_enabled():
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            content = await _openrouter_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                format=format,
                think=think,
            )
        except Exception as e:
            _record_fallback(e)
        else:
            _record_remote_success()
            return content
    return await ollama.generate(
        prompt=prompt,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        think=think,
        format=format,
    )

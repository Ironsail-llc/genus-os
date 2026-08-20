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

- ``ROBOTHOR_MEMORY_GENERATION_MIN_INTERVAL_S``: minimum interval between
  remote calls (default 1.5s, ``0`` disables) — smooths nightly batches
  under provider rate limits.

Failure policy (audit lesson: silent fallbacks hide primary-model death):

- 429/503 → retry up to ``REMOTE_RATE_LIMIT_MAX_ATTEMPTS`` attempts with
  jittered exponential backoff, honoring a finite ``Retry-After`` (each
  sleep capped at 20s, total sleep budget 45s — memory writes tolerate
  latency; incident 2026-08-19: a nightly batch abandoned remote in 20s
  because every 429 fell back after a single attempt).
- Timeouts / network errors → one retry, then fallback.
- Any other error (4xx included) → fail fast.
- Remote abandoned → fall back to local Ollama, log a WARNING containing
  ``MEMORY_GENERATION_REMOTE_FALLBACK`` and increment
  ``remote_fallback_count``. Never silent, and never before retries are
  exhausted.
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
import time
from typing import Any

import httpx

from robothor.llm import ollama

logger = logging.getLogger(__name__)

PROVIDER_ENV = "ROBOTHOR_MEMORY_GENERATION_PROVIDER"
REMOTE_MODEL_ENV = "ROBOTHOR_MEMORY_GENERATION_REMOTE_MODEL"
MIN_INTERVAL_ENV = "ROBOTHOR_MEMORY_GENERATION_MIN_INTERVAL_S"
DEFAULT_REMOTE_MODEL = "openrouter/xiaomi/mimo-v2.5"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
REMOTE_TIMEOUT_S = 60.0

# Retry budget for provider back-pressure (429) and brief unavailability
# (503). Memory writes tolerate latency, so waiting beats abandoning remote:
# 3 attempts of base=2s * factor=3 (+/-25% jitter) sleep ~8s typical; a
# Retry-After header may push each sleep to the 20s cap, bounded overall by
# the 45s total budget. Other 4xx are deterministic → fail fast; timeouts
# and network errors get exactly one retry.
REMOTE_RATE_LIMIT_MAX_ATTEMPTS = 3
REMOTE_TRANSPORT_MAX_ATTEMPTS = 2
REMOTE_BACKOFF_BASE_SECONDS = 2.0
REMOTE_BACKOFF_FACTOR = 3.0
REMOTE_BACKOFF_CAP_SECONDS = 20.0
REMOTE_BACKOFF_BUDGET_SECONDS = 45.0
REMOTE_BACKOFF_JITTER_FRACTION = 0.25

# Minimum interval between remote calls (seconds). Nightly memory batches
# fire dozens of back-to-back generations; pacing them keeps the batch under
# provider rate limits instead of triggering a 429 storm.
DEFAULT_MIN_INTERVAL_S = 1.5

_RETRYABLE_STATUS_CODES = frozenset({429, 503})

# Same transport-failure taxonomy as robothor.llm.ollama: timeouts,
# connect/read/write resets, and the server hanging up mid-response.
# LocalProtocolError/UnsupportedProtocol are client-side bugs — not retried.
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

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

# Log the missing-key ERROR once per process, not once per memory write.
_missing_key_logged: bool = False

# Inter-call pacing state. The engine runs a single event loop, so a
# monotonic timestamp guarded by one asyncio.Lock is sufficient. The lock is
# created lazily: asyncio primitives bind to the running loop on first use,
# and import time has no loop.
_pacing_lock: asyncio.Lock | None = None
_last_remote_call_at: float = 0.0

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


def _min_interval_s() -> float:
    """Minimum seconds between remote calls; 0 disables pacing.

    Garbage, non-finite, or negative env values fall back to the default —
    a typo must never disable pacing or wedge the loop.
    """
    raw = os.environ.get(MIN_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_MIN_INTERVAL_S
    try:
        value = float(raw.strip())
    except ValueError:
        return DEFAULT_MIN_INTERVAL_S
    if not math.isfinite(value) or value < 0:
        return DEFAULT_MIN_INTERVAL_S
    return value


def _get_pacing_lock() -> asyncio.Lock:
    global _pacing_lock
    if _pacing_lock is None:
        _pacing_lock = asyncio.Lock()
    return _pacing_lock


async def _pace_remote_call() -> None:
    """Space remote calls at least ``_min_interval_s()`` apart.

    Each caller reserves the next available slot under the lock, then sleeps
    outside it — concurrent callers queue up at interval spacing instead of
    bursting into the provider's rate limit.
    """
    interval = _min_interval_s()
    if interval <= 0:
        return
    global _last_remote_call_at
    async with _get_pacing_lock():
        now = time.monotonic()
        scheduled = max(now, _last_remote_call_at + interval)
        _last_remote_call_at = scheduled
    wait = scheduled - now
    if wait > 0:
        await asyncio.sleep(wait)


def _remote_backoff_seconds(attempt: int, error: Exception | None = None) -> float:
    """Jittered exponential backoff for remote retry ``attempt`` (0-indexed).

    Honors a Retry-After header on an HTTPStatusError when present, clamped
    to the per-sleep cap so a misbehaving server can't blow the retry
    budget. Mirrors robothor.llm.ollama._embed_backoff_seconds.
    """
    if isinstance(error, httpx.HTTPStatusError):
        retry_after = error.response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                seconds: float | None = float(retry_after)
            except ValueError:
                seconds = None
            # Guard non-finite values ("nan"/"inf"): nan slips through
            # min/max clamps and would reach asyncio.sleep(nan).
            if seconds is not None and math.isfinite(seconds):
                return min(max(seconds, 0.0), REMOTE_BACKOFF_CAP_SECONDS)

    base = REMOTE_BACKOFF_BASE_SECONDS * (REMOTE_BACKOFF_FACTOR**attempt)
    capped = min(base, REMOTE_BACKOFF_CAP_SECONDS)
    jitter = capped * REMOTE_BACKOFF_JITTER_FRACTION
    return capped + random.uniform(-jitter, jitter)


def _record_fallback(error: Exception) -> None:
    global remote_fallback_count
    remote_fallback_count += 1
    logger.warning(
        "%s #%d: remote memory generation via %s failed (%s: %s) — falling back to local ollama",
        FALLBACK_MARKER,
        remote_fallback_count,
        _remote_model(),
        type(error).__name__,
        error,
    )


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
    async with httpx.AsyncClient(timeout=REMOTE_TIMEOUT_S) as client:
        resp = await client.post(OPENROUTER_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    content = strip_think_blocks(data["choices"][0]["message"]["content"] or "")
    if not content:
        raise RuntimeError(f"empty content from remote model {payload['model']}")
    logger.info(
        "remote memory generation: %d content chars via %s",
        len(content),
        payload["model"],
    )
    return content


async def _openrouter_chat_with_retry(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    format: Any | None,  # noqa: A002 — parity with ollama.chat
    think: bool = True,
) -> str:
    """_openrouter_chat with pacing and retry-on-back-pressure.

    Raises only when remote is truly abandoned, so the caller's loud
    fallback (FALLBACK_MARKER) fires exactly once per abandoned call —
    never on an interim retry.
    """
    slept_total = 0.0
    rate_limit_attempts = 0
    transport_attempts = 0
    while True:
        await _pace_remote_call()
        try:
            return await _openrouter_chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                format=format,
                think=think,
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status not in _RETRYABLE_STATUS_CODES:
                raise  # deterministic 4xx/5xx — fail fast to fallback
            rate_limit_attempts += 1
            if rate_limit_attempts >= REMOTE_RATE_LIMIT_MAX_ATTEMPTS:
                raise
            wait = _remote_backoff_seconds(rate_limit_attempts - 1, e)
            if slept_total + wait > REMOTE_BACKOFF_BUDGET_SECONDS:
                raise  # sleep budget exhausted — abandon remote
            attempt, max_attempts = rate_limit_attempts, REMOTE_RATE_LIMIT_MAX_ATTEMPTS
            label = f"HTTP {status}"
        except _RETRYABLE_TRANSPORT_ERRORS as e:
            transport_attempts += 1
            if transport_attempts >= REMOTE_TRANSPORT_MAX_ATTEMPTS:
                raise
            wait = _remote_backoff_seconds(transport_attempts - 1)
            if slept_total + wait > REMOTE_BACKOFF_BUDGET_SECONDS:
                raise  # sleep budget exhausted — abandon remote
            attempt, max_attempts = transport_attempts, REMOTE_TRANSPORT_MAX_ATTEMPTS
            label = type(e).__name__

        # Interim retry: WARNING but deliberately without FALLBACK_MARKER.
        logger.warning(
            "remote memory generation attempt %d/%d failed (%s) — retrying in %.1fs",
            attempt,
            max_attempts,
            label,
            wait,
        )
        await asyncio.sleep(wait)
        slept_total += wait


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
            return await _openrouter_chat_with_retry(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                format=format,
                think=think,
            )
        except Exception as e:
            _record_fallback(e)
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
            return await _openrouter_chat_with_retry(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                format=format,
                think=think,
            )
        except Exception as e:
            _record_fallback(e)
    return await ollama.generate(
        prompt=prompt,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        think=think,
        format=format,
    )

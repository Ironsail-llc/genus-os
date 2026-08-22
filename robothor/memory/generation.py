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

Alerting (incident 2026-08-21: fact extraction failed continuously for days
behind ~729 HTTP-429 + ~93 HTTP-503 events, and every signal was a log line
nobody read):

- Fallback streak past ``FALLBACK_STREAK_THRESHOLD`` → one **warning**
  alert (digest, not a page): remote is down, the load is back on the GPU.
- Local leg fails too → one **critical** alert: no generation path is left,
  so memory writes are being dropped. Callers swallow this exception
  (``extract_facts`` logs "failed after N attempts" and returns ``[]``), so
  this seam is the last place that can still tell the operator.

Both alerts are latched (``ALERT_RELATCH_SECONDS``) and released by a
success on the corresponding leg, so a storm produces one alert, not
thousands.

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
from typing import TYPE_CHECKING, Any

import httpx

from robothor.llm import ollama

if TYPE_CHECKING:
    from collections.abc import Awaitable

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

# Consecutive remote→local fallbacks (reset on any remote success). A
# sustained streak means the GPU offload the remote path exists for is
# defeated — escalate to ERROR with a grep-able marker so the operator
# looks at the remote provider instead of the symptom (GPU load).
FALLBACK_STREAK_THRESHOLD = 5
FALLBACK_STREAK_MARKER = "MEMORY_GENERATION_REMOTE_FALLBACK_STREAK"
_consecutive_fallbacks: int = 0

# ── Alert latches ───────────────────────────────────────────────────────
#
# A degraded provider produces hundreds of events an hour, so each alert
# condition fires at most once per ``ALERT_RELATCH_SECONDS`` and re-arms
# immediately on the matching recovery (remote success releases the streak
# latch; any successful generation releases the down latch). In-process
# state, like robothor.engine.detectors._dedup: a restart clears it, which
# is fine — an ongoing condition re-fires on the next call.
ALERT_RELATCH_SECONDS = 3600.0
FALLBACK_STREAK_ALERT_KEY = "memory_generation_fallback_streak"
GENERATION_DOWN_ALERT_KEY = "memory_generation_down"
_alert_latched_at: dict[str, float] = {}

# Alert bodies quote the provoking error; keep them readable in a page.
_ALERT_ERROR_CHARS = 200

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
    global remote_fallback_count, _consecutive_fallbacks
    remote_fallback_count += 1
    _consecutive_fallbacks += 1
    logger.warning(
        "%s #%d: remote memory generation via %s failed (%s: %s) — falling back to local ollama",
        FALLBACK_MARKER,
        remote_fallback_count,
        _remote_model(),
        type(error).__name__,
        error,
    )
    if _consecutive_fallbacks % FALLBACK_STREAK_THRESHOLD == 0:
        logger.error(
            "%s: %d consecutive remote fallbacks — remote memory generation is"
            " effectively down and the load is back on the local GPU",
            FALLBACK_STREAK_MARKER,
            _consecutive_fallbacks,
        )


def _record_remote_success() -> None:
    global _consecutive_fallbacks
    _consecutive_fallbacks = 0
    _release_latch(FALLBACK_STREAK_ALERT_KEY)
    _release_latch(GENERATION_DOWN_ALERT_KEY)


def _latch(key: str) -> bool:
    """True when ``key`` may fire now; marks it latched until it re-arms."""
    now = time.monotonic()
    last = _alert_latched_at.get(key)
    if last is not None and now - last < ALERT_RELATCH_SECONDS:
        return False
    _alert_latched_at[key] = now
    return True


def _release_latch(key: str) -> None:
    """Re-arm ``key`` after the condition it describes has recovered."""
    _alert_latched_at.pop(key, None)


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {str(error)[:_ALERT_ERROR_CHARS]}"


async def _fire_alert(
    key: str,
    level: str,
    title: str,
    body: str,
    metadata: dict[str, Any],
) -> None:
    """Raise a latched alert through the engine's alert path.

    ``robothor.engine.alerts`` is imported lazily inside the function: the
    engine imports the memory package, so a module-level import here would
    point the dependency both ways and close an import cycle.

    Alerting is strictly best-effort — a broken alert channel must never
    turn a degraded memory write into a raised exception.
    """
    if not _latch(key):
        return
    try:
        from robothor.engine.alerts import alert

        delivered = await alert(level, title, body, metadata=metadata)
        if not delivered:
            logger.warning("memory generation alert not delivered (%s): %s", level, title)
    except Exception as e:
        logger.warning("memory generation alert dispatch failed (%s): %s", title, e)


async def _note_fallback(error: Exception) -> None:
    """Record a remote→local fallback and alert once a streak is established.

    One fallback is business as usual — local absorbs it. A streak means the
    remote provider is effectively down: warning level, so it lands in the
    digest the briefing reads instead of paging at 3am for something that
    still produces memories, just slowly.
    """
    _record_fallback(error)
    if _consecutive_fallbacks < FALLBACK_STREAK_THRESHOLD:
        return
    remote_model = _remote_model()
    await _fire_alert(
        FALLBACK_STREAK_ALERT_KEY,
        "warning",
        "Memory generation: remote provider down",
        (
            f"{_consecutive_fallbacks} consecutive remote→local fallbacks.\n"
            f"remote: {remote_model} (provider={_provider()}) — {_describe(error)}\n"
            f"local: ollama {ollama.GENERATION_MODEL} is absorbing every memory write.\n"
            f"Check the provider's rate limits/quota and {REMOTE_MODEL_ENV}; "
            f"grep the journal for {FALLBACK_MARKER}."
        ),
        {
            "consecutive_fallbacks": _consecutive_fallbacks,
            "remote_fallback_count": remote_fallback_count,
            "remote_model": remote_model,
            "local_model": ollama.GENERATION_MODEL,
            "error_type": type(error).__name__,
        },
    )


async def _local_leg(call: Awaitable[str], remote_error: Exception | None) -> str:
    """Await the local Ollama leg, paging when it fails too.

    A remote failure alone is survivable — local absorbs it. When local
    fails as well there is no generation path left and the memory write is
    dropped: every caller swallows this exception, so the alert raised here
    is the only thing that reaches the operator.
    """
    try:
        result: str = await call
    except Exception as local_error:
        remote_line = (
            f"remote: {_remote_model()} — {_describe(remote_error)}"
            if remote_error is not None
            else f"remote: not attempted (provider={_provider()})"
        )
        await _fire_alert(
            GENERATION_DOWN_ALERT_KEY,
            "critical",
            "Memory generation is down",
            (
                "Both memory generation legs failed — fact extraction, episode "
                "summaries and insight discovery are being dropped on the floor.\n"
                f"{remote_line}\n"
                f"local: ollama {ollama.GENERATION_MODEL} — {_describe(local_error)}\n"
                "Check the Ollama service and the remote provider; until one "
                "recovers nothing is being written to memory."
            ),
            {
                "remote_model": _remote_model(),
                "local_model": ollama.GENERATION_MODEL,
                "remote_error_type": (
                    type(remote_error).__name__ if remote_error is not None else None
                ),
                "local_error_type": type(local_error).__name__,
            },
        )
        raise
    _release_latch(GENERATION_DOWN_ALERT_KEY)
    return result


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

    return content_from_response(data, model=payload["model"])


#: Token budget for one fact extraction. Was a bare 1024 at facts.py:268, which
#: truncated 59% of production extractions (72 of 122 over 7 days parsed zero
#: facts). The two populations barely overlap: zero-fact responses ran
#: 2654-3863 chars against a ~3.5-4k ceiling, while successful ones ran
#: 491-3394. The failures were not short conversations — they were cut off
#: mid-JSON. Same shape as the starved benchmark judge in #335.
EXTRACTION_MAX_TOKENS = 4096

#: Same failure at a smaller ceiling: insight discovery ran at 512 and its parse
#: errors cluster at char ~1480-1590 ("Unterminated string", "Expecting ','"),
#: which is a JSON object cut in half rather than a bad answer.
INSIGHT_MAX_TOKENS = 2048


def content_from_response(data: dict[str, Any], *, model: str) -> str:
    """Text from a chat completion, refusing anything that was cut short.

    A ``finish_reason`` of ``length`` means the model ran out of budget partway
    through. The body that comes back is long and unparseable, and treating it
    as an answer is how "the conversation contained no facts" got recorded 72
    times in a week. Raise instead, so the retry path sees a failure.
    """
    choice = (data.get("choices") or [{}])[0]
    content = strip_think_blocks((choice.get("message") or {}).get("content") or "")
    finish_reason = choice.get("finish_reason")

    if finish_reason == "length":
        raise RuntimeError(
            f"truncated response from remote model {model}: finish_reason=length "
            f"after {len(content)} chars — raise max_tokens (currently "
            f"{EXTRACTION_MAX_TOKENS} for extraction)"
        )
    if not content:
        raise RuntimeError(f"empty content from remote model {model}")

    logger.info("remote memory generation: %d content chars via %s", len(content), model)
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
    remote_error: Exception | None = None
    if _remote_enabled():
        try:
            result = await _openrouter_chat_with_retry(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                format=format,
                think=think,
            )
        except Exception as e:
            remote_error = e
            await _note_fallback(e)
        else:
            _record_remote_success()
            return result
    return await _local_leg(
        ollama.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            think=think,
            format=format,
        ),
        remote_error,
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
    remote_error: Exception | None = None
    if _remote_enabled():
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            result = await _openrouter_chat_with_retry(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                format=format,
                think=think,
            )
        except Exception as e:
            remote_error = e
            await _note_fallback(e)
        else:
            _record_remote_success()
            return result
    return await _local_leg(
        ollama.generate(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            think=think,
            format=format,
        ),
        remote_error,
    )

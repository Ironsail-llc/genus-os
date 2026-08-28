"""
LLM Client for local models via Ollama.

Provides async generation capabilities using local inference.
100% local — no cloud APIs.

For structured output tasks (JSON extraction, classification), use the
`format` parameter to pass a JSON schema. Ollama constrains generation
to match the schema, producing reliable structured output.

Usage:
    from robothor.llm.ollama import generate, get_embedding_async

    # Text generation
    result = await generate("What is 2+2?")

    # Structured output
    result = await generate("Extract facts...", format={"type": "array", ...})

    # Embeddings
    embedding = await get_embedding_async("Some text")
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from robothor.llm.local_gate import Lane

logger = logging.getLogger(__name__)

# Test seam: set to an httpx.MockTransport to intercept requests without
# touching the network. Left None (the httpx default) in production.
_transport: httpx.AsyncBaseTransport | None = None

# chat() retry budget — Ollama is on localhost; transient failures (a model
# reload, a brief 5xx/429 blip) are short-lived, so two attempts is enough.
CHAT_MAX_ATTEMPTS = 2
CHAT_RETRY_DELAY_SECONDS = 3.0
#: Spread applied to the retry above. A fixed delay makes every caller that hit a
#: busy server retry in lockstep, which is how backpressure becomes a thundering
#: herd against a device serving one inference slot.
CHAT_RETRY_JITTER_FRACTION = 0.5


def _chat_retry_delay() -> float:
    spread = CHAT_RETRY_DELAY_SECONDS * CHAT_RETRY_JITTER_FRACTION
    return max(0.0, CHAT_RETRY_DELAY_SECONDS + random.uniform(-spread, spread))

# Embedding retry budget — sized to survive a multi-minute 5xx/timeout storm
# (e.g. the model being evicted and reloaded) without burning the caller's
# whole request budget. 5 attempts of base=2s * factor=3 (capped at 45s,
# +/-25% jitter) sum to ~71s uncapped / ~89s worst-case jittered — comfortably
# under the ~2 minute target.
EMBED_MAX_ATTEMPTS = 5
EMBED_BACKOFF_BASE_SECONDS = 2.0
EMBED_BACKOFF_FACTOR = 3.0
EMBED_BACKOFF_CAP_SECONDS = 45.0
EMBED_BACKOFF_JITTER_FRACTION = 0.25


def _probe_client(timeout: float) -> httpx.AsyncClient:
    """An UNGATED client, for cheap metadata calls only (``/api/tags``).

    Availability probes must never queue behind inference: a probe that waits for a
    busy GPU reports the model as missing, and callers degrade as though it were.
    """
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


@asynccontextmanager
async def _client(timeout: float, lane: Lane | None = None):
    """One inference slot, then a client. Every request-bearing call goes through here.

    This is the single funnel for local inference in this module, so gating it covers
    the memory pipeline, chat history embedding, RAG generation and the self-model in
    one place. See ``robothor/llm/local_gate.py`` for why heat, not money, is the
    scarce resource being rationed.
    """
    from robothor.llm.local_gate import Lane as _Lane
    from robothor.llm.local_gate import gate

    async with gate().slot(lane=lane or _Lane.NORMAL):
        async with httpx.AsyncClient(timeout=timeout, transport=_transport) as client:
            yield client


# Transport-level failures worth retrying: timeouts, connect/read/write resets
# (NetworkError covers ConnectError/ReadError/WriteError/CloseError), and the
# server hanging up mid-response (RemoteProtocolError — exactly what an Ollama
# restart mid-request produces). LocalProtocolError/UnsupportedProtocol are
# client-side bugs and stay non-retryable.
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


def _is_retryable_embed_error(e: Exception) -> bool:
    """5xx, 429, and transport-level failures are retryable; other 4xx are not."""
    if isinstance(e, _RETRYABLE_TRANSPORT_ERRORS):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        return status == 429 or status >= 500
    return False


def _embed_backoff_seconds(attempt: int, error: Exception | None = None) -> float:
    """Jittered exponential backoff for embed retry `attempt` (0-indexed).

    Honors a Retry-After header on an HTTPStatusError when present, clamped
    to the same cap so a misbehaving server can't blow the retry budget.
    """
    if isinstance(error, httpx.HTTPStatusError):
        retry_after = error.response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                seconds = float(retry_after)
            except ValueError:
                seconds = None
            # Guard non-finite values ("nan"/"inf"): nan slips through
            # min/max clamps and would reach asyncio.sleep(nan).
            if seconds is not None and math.isfinite(seconds):
                return min(max(seconds, 0.0), EMBED_BACKOFF_CAP_SECONDS)

    base = EMBED_BACKOFF_BASE_SECONDS * (EMBED_BACKOFF_FACTOR**attempt)
    capped = min(base, EMBED_BACKOFF_CAP_SECONDS)
    jitter = capped * EMBED_BACKOFF_JITTER_FRACTION
    return capped + random.uniform(-jitter, jitter)


def _keep_alive_for(model_class: str) -> str:
    """Get the keep_alive duration for a model class (generation/embedding/vision)."""
    try:
        from robothor.config import get_config

        cfg = get_config().ollama
        return getattr(cfg, f"keep_alive_{model_class}", "5m")
    except Exception:
        return "5m"


def _ollama_url() -> str:
    """Get Ollama URL from config or env."""
    url = os.environ.get("ROBOTHOR_OLLAMA_URL") or os.environ.get("OLLAMA_URL")
    if url:
        return url
    try:
        from robothor.config import get_config

        return get_config().ollama.base_url
    except Exception:
        return "http://localhost:11434"


def _num_ctx_option() -> int | None:
    """Optional per-request context-window clamp (ROBOTHOR_OLLAMA_NUM_CTX).

    The Ollama *server's* context-length default can over-ask a model — the
    2026-08-20 journal showed num_ctx=262144 requested against a model with
    n_ctx_train=40960 ("requested context size too large for model"), wasting
    VRAM. No repo code sends num_ctx, so the server default applies unless the
    operator sets this env to the model's training context; a per-request
    ``options.num_ctx`` overrides the server default. Unset (the default)
    keeps current behavior.
    """
    raw = os.environ.get("ROBOTHOR_OLLAMA_NUM_CTX", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ROBOTHOR_OLLAMA_NUM_CTX=%r is not an integer — ignoring", raw)
        return None
    if value <= 0:
        logger.warning("ROBOTHOR_OLLAMA_NUM_CTX=%d is not positive — ignoring", value)
        return None
    return value


def _apply_num_ctx(options: dict[str, Any]) -> dict[str, Any]:
    """Add the num_ctx clamp to a generation options dict when configured."""
    num_ctx = _num_ctx_option()
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    return options


def _embedding_model() -> str:
    """Get embedding model name."""
    model = os.environ.get("ROBOTHOR_EMBEDDING_MODEL")
    if model:
        return model
    try:
        from robothor.config import get_config

        return get_config().ollama.embedding_model
    except Exception:
        return "qwen3-embedding:0.6b"


# Default generation model — updated by detect_generation_model()
# Default must agree with robothor/config.py and infra/robothor.env.example.
GENERATION_MODEL = os.environ.get("ROBOTHOR_GENERATION_MODEL", "qwen3:8b")

# Model preferences for auto-detection (in order)
GENERATION_MODEL_PREFERENCES = [
    "qwen3:32b",
    "llama3.2-vision:11b",
    "llama3.2",
    "llama3.2:3b",
]


async def generate(
    prompt: str,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    stream: bool = False,
    model: str | None = None,
    think: bool = True,
    format: Any | None = None,  # noqa: A002
) -> str:
    """Generate a response from local LLM via Ollama.

    Args:
        prompt: The user prompt.
        system: Optional system prompt.
        temperature: Sampling temperature (0.0-1.0).
        max_tokens: Maximum tokens to generate.
        stream: If True, returns an async generator of chunks.
        model: Override the default model.
        think: If True (default), model reasons in a separate field.
        format: JSON schema dict for structured output.

    Returns:
        The generated text response.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    return await chat(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        think=think,
        format=format,
    )


async def generate_stream(
    prompt: str,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    model: str | None = None,
    think: bool = True,
) -> AsyncGenerator[str, None]:
    """Stream a response from local LLM via Ollama."""
    effective_temp = max(temperature, 0.6) if think else temperature
    payload = {
        "model": model or GENERATION_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": _keep_alive_for("generation"),
        "options": _apply_num_ctx(
            {
                "temperature": effective_temp,
                "num_predict": max_tokens,
                "top_p": 0.95,
                "top_k": 20,
                "repeat_penalty": 1.0,
                "num_gpu": 999,
            }
        ),
    }
    if system:
        payload["system"] = system

    url = _ollama_url()
    async with _client(60.0) as client:
        async with client.stream("POST", f"{url}/api/generate", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.strip():
                    data = json.loads(line)
                    if data.get("response"):
                        yield data["response"]
                    if data.get("done"):
                        break


async def chat(
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 4096,
    model: str | None = None,
    think: bool = True,
    format: Any | None = None,  # noqa: A002
) -> str:
    """Chat completion via Ollama /api/chat endpoint.

    Args:
        messages: List of {"role": "user"|"assistant"|"system", "content": "..."}.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens to generate.
        model: Override the default model.
        think: If True (default), model reasons in a separate 'thinking' field.
        format: JSON schema dict for structured output.

    Returns:
        The assistant's reply (content field only, thinking is separated).
    """
    if think:
        thinking_overhead = 8192
        effective_tokens = max_tokens + thinking_overhead
        effective_temp = max(temperature, 0.6)
    else:
        effective_tokens = max_tokens
        effective_temp = temperature

    effective_model = model or GENERATION_MODEL

    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
        "stream": False,
        "keep_alive": _keep_alive_for("generation"),
        "options": _apply_num_ctx(
            {
                "temperature": effective_temp,
                "num_predict": effective_tokens,
                "top_p": 0.95,
                "top_k": 20,
                "repeat_penalty": 1.0,
                "num_gpu": 999,
            }
        ),
    }

    # Only add think parameter for Qwen models (others don't support it)
    if "qwen" in effective_model.lower():
        payload["think"] = think

    if format is not None:
        payload["format"] = format

    url = _ollama_url()
    last_error: Exception | None = None
    for attempt in range(CHAT_MAX_ATTEMPTS):
        is_last = attempt == CHAT_MAX_ATTEMPTS - 1
        try:
            async with _client(180.0) as client:
                resp = await client.post(f"{url}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
                content: str = data["message"]["content"]
                done_reason = data.get("done_reason", "unknown")
                eval_count = data.get("eval_count", 0)
                thinking = data["message"].get("thinking", "")
                logger.info(
                    "chat: %d thinking chars, %d content chars, %d eval tokens, done=%s",
                    len(thinking) if thinking else 0,
                    len(content),
                    int(eval_count),
                    str(done_reason).replace("\n", "\\n"),
                )
                if done_reason == "length" and not content.strip():
                    logger.warning(
                        "Thinking exhausted token budget (eval=%d, num_predict=%d)",
                        int(eval_count),
                        int(payload["options"]["num_predict"]),
                    )
                return content
        except _RETRYABLE_TRANSPORT_ERRORS as e:
            last_error = e
            if is_last:
                logger.warning(
                    "Chat attempt %d/%d failed (transient, giving up): %s",
                    attempt + 1,
                    CHAT_MAX_ATTEMPTS,
                    e,
                )
            else:
                logger.warning(
                    "Chat attempt %d/%d failed (transient): %s (retrying in %ds)",
                    attempt + 1,
                    CHAT_MAX_ATTEMPTS,
                    e,
                    CHAT_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(_chat_retry_delay())
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status >= 500 or status == 429:
                last_error = e
                if is_last:
                    logger.warning(
                        "Chat attempt %d/%d failed (%d, giving up): %s",
                        attempt + 1,
                        CHAT_MAX_ATTEMPTS,
                        status,
                        e,
                    )
                else:
                    logger.warning(
                        "Chat attempt %d/%d failed (%d): %s (retrying in %ds)",
                        attempt + 1,
                        CHAT_MAX_ATTEMPTS,
                        status,
                        e,
                        CHAT_RETRY_DELAY_SECONDS,
                    )
                    await asyncio.sleep(_chat_retry_delay())
            else:
                raise  # non-retryable 4xx
    raise last_error  # type: ignore[misc]


async def chat_stream(
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 4096,
    model: str | None = None,
    think: bool = True,
) -> AsyncGenerator[str, None]:
    """Stream a chat completion via Ollama."""
    effective_temp = max(temperature, 0.6) if think else temperature
    payload = {
        "model": model or GENERATION_MODEL,
        "messages": messages,
        "stream": True,
        "think": think,
        "keep_alive": _keep_alive_for("generation"),
        "options": _apply_num_ctx(
            {
                "temperature": effective_temp,
                "num_predict": max_tokens,
                "top_p": 0.95,
                "top_k": 20,
                "repeat_penalty": 1.0,
                "num_gpu": 999,
            }
        ),
    }

    url = _ollama_url()
    async with _client(60.0) as client:
        async with client.stream("POST", f"{url}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.strip():
                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if data.get("done"):
                        break


async def analyze_image(
    image_base64: str,
    prompt: str = "Describe what you see in this image.",
    system: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
) -> str:
    """Analyze an image using the vision-capable LLM via Ollama.

    Args:
        image_base64: Base64-encoded image data (JPEG or PNG).
        prompt: What to analyze in the image.
        system: Optional system prompt for context.
        temperature: Sampling temperature (lower = more deterministic).
        max_tokens: Maximum tokens to generate.

    Returns:
        The model's description/analysis of the image.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append(
        {
            "role": "user",
            "content": prompt,
            "images": [image_base64],
        }
    )

    payload = {
        "model": "llama3.2-vision:11b",
        "messages": messages,
        "stream": False,
        "keep_alive": _keep_alive_for("vision"),
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_gpu": 999,
        },
    }

    url = _ollama_url()
    async with _client(60.0) as client:
        resp = await client.post(f"{url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        content: str = data["message"]["content"]
        eval_count = data.get("eval_count", 0)
        logger.info("analyze_image: %d content chars, %d eval tokens", len(content), eval_count)
        return content


async def get_embedding_async(
    text: str, model: str | None = None, max_retries: int = EMBED_MAX_ATTEMPTS
) -> list[float]:
    """Get embedding vector via Ollama (async version) with retry."""
    payload = {
        "model": model or _embedding_model(),
        "input": text,
        "keep_alive": _keep_alive_for("embedding"),
    }
    url = _ollama_url()
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with _client(30.0) as client:
                resp = await client.post(f"{url}/api/embed", json=payload)
                resp.raise_for_status()
                embeddings: list[float] = resp.json()["embeddings"][0]
                return embeddings
        except (httpx.HTTPStatusError, *_RETRYABLE_TRANSPORT_ERRORS) as e:
            if not _is_retryable_embed_error(e):
                raise
            last_error = e
            is_last = attempt == max_retries - 1
            if is_last:
                logger.warning(
                    "Embedding attempt %d/%d failed (giving up): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
            else:
                wait = _embed_backoff_seconds(attempt, e)
                logger.warning(
                    "Embedding attempt %d/%d failed: %s (retrying in %.1fs)",
                    attempt + 1,
                    max_retries,
                    e,
                    wait,
                )
                await asyncio.sleep(wait)
    raise last_error  # type: ignore[misc]


async def get_embeddings_batch_async(
    texts: list[str],
    model: str | None = None,
) -> list[list[float]]:
    """Get embedding vectors for multiple texts in one Ollama call.

    Ollama's /api/embed supports batch input via ``"input": [text1, text2, ...]``.
    Falls back to sequential single-text calls if the batch request fails.

    Args:
        texts: List of texts to embed.
        model: Override the default embedding model.

    Returns:
        List of embedding vectors (one per input text).
    """
    if not texts:
        return []
    if len(texts) == 1:
        emb = await get_embedding_async(texts[0], model=model)
        return [emb]

    payload = {
        "model": model or _embedding_model(),
        "input": texts,
        "keep_alive": _keep_alive_for("embedding"),
    }
    url = _ollama_url()
    last_error: Exception | None = None
    for attempt in range(EMBED_MAX_ATTEMPTS):
        try:
            async with _client(180.0) as client:
                resp = await client.post(f"{url}/api/embed", json=payload)
                resp.raise_for_status()
                embeddings: list[list[float]] = resp.json()["embeddings"]
                if len(embeddings) == len(texts):
                    logger.info("batch embed: %d texts in one call", len(texts))
                    return embeddings
                logger.warning(
                    "batch embed returned %d embeddings for %d texts, falling back",
                    len(embeddings),
                    len(texts),
                )
                break  # Wrong count — fall through to sequential
        except (httpx.HTTPStatusError, *_RETRYABLE_TRANSPORT_ERRORS) as e:
            if not _is_retryable_embed_error(e):
                raise
            last_error = e
            is_last = attempt == EMBED_MAX_ATTEMPTS - 1
            if is_last:
                logger.warning(
                    "Batch embed attempt %d/%d failed (giving up): %s",
                    attempt + 1,
                    EMBED_MAX_ATTEMPTS,
                    e,
                )
            else:
                wait = _embed_backoff_seconds(attempt, e)
                logger.warning(
                    "Batch embed attempt %d/%d failed: %s (retrying in %.1fs)",
                    attempt + 1,
                    EMBED_MAX_ATTEMPTS,
                    e,
                    wait,
                )
                await asyncio.sleep(wait)
        except Exception as e:
            logger.warning("batch embed failed (%s), falling back to sequential", e)
            break
    else:
        logger.warning(
            "batch embed failed after %d attempts (%s), falling back to sequential",
            EMBED_MAX_ATTEMPTS,
            last_error,
        )

    # Fallback: sequential single-text calls
    results = []
    for text in texts:
        emb = await get_embedding_async(text, model=model)
        results.append(emb)
    return results


async def check_model_available(model: str | None = None) -> bool:
    """Check if a model is available in Ollama."""
    try:
        url = _ollama_url()
        async with _probe_client(5.0) as client:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            target = model or GENERATION_MODEL
            return any(target in m or m.startswith(target.split(":")[0]) for m in models)
    except Exception:
        return False


async def detect_generation_model() -> str | None:
    """Auto-detect the best available generation model from Ollama."""
    global GENERATION_MODEL
    try:
        url = _ollama_url()
        async with _probe_client(5.0) as client:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]

            for pref in GENERATION_MODEL_PREFERENCES:
                for m in models:
                    if pref in m or m.startswith(pref.split(":")[0] + ":"):
                        GENERATION_MODEL = m
                        found: str = m
                        return found

            for m in models:
                if "qwen3" in m.lower() and "embed" not in m.lower() and "rerank" not in m.lower():
                    GENERATION_MODEL = m
                    fallback: str = m
                    return fallback

            return None
    except Exception:
        return None


# Synchronous wrappers for CLI usage
def generate_sync(prompt: str, **kwargs: Any) -> str:
    """Synchronous wrapper around generate()."""
    return asyncio.run(generate(prompt, **kwargs))


def chat_sync(messages: list[dict[str, str]], **kwargs: Any) -> str:
    """Synchronous wrapper around chat()."""
    return asyncio.run(chat(messages, **kwargs))

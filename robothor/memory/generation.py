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

import logging
import os
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

# Distinctive log markers — grep targets for alerting.
FALLBACK_MARKER = "MEMORY_GENERATION_REMOTE_FALLBACK"
MISSING_KEY_MARKER = "MEMORY_GENERATION_REMOTE_MISCONFIGURED"

# Module counter: number of remote-generation calls that fell back to local.
remote_fallback_count: int = 0

# Log the missing-key ERROR once per process, not once per memory write.
_missing_key_logged: bool = False

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
) -> str:
    """One OpenRouter chat-completions call, normalized to Ollama semantics."""
    payload: dict[str, Any] = {
        "model": _remote_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
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
            return await _openrouter_chat(
                messages, temperature=temperature, max_tokens=max_tokens, format=format
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
            return await _openrouter_chat(
                messages, temperature=temperature, max_tokens=max_tokens, format=format
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

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
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _ollama_url() -> str:
    """Get Ollama URL from config or env."""
    url = os.environ.get("ROBOTHOR_OLLAMA_URL") or os.environ.get("OLLAMA_URL")
    if url:
        return url
    try:
        from robothor.config import get_config
        cfg_url: str = get_config().ollama.url  # type: ignore[attr-defined]
        return cfg_url
    except Exception:
        return "http://localhost:11434"


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
GENERATION_MODEL = os.environ.get("ROBOTHOR_GENERATION_MODEL", "llama3.2-vision:11b")

# Model preferences for auto-detection (in order)
GENERATION_MODEL_PREFERENCES = [
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
    format: Any | None = None,
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
        "options": {
            "temperature": effective_temp,
            "num_predict": max_tokens,
            "top_p": 0.95,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "num_gpu": 999,
        },
    }
    if system:
        payload["system"] = system

    url = _ollama_url()
    async with httpx.AsyncClient(timeout=300.0) as client:
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
    format: Any | None = None,
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
        "options": {
            "temperature": effective_temp,
            "num_predict": effective_tokens,
            "top_p": 0.95,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "num_gpu": 999,
        },
    }

    # Only add think parameter for Qwen models (others don't support it)
    if "qwen" in effective_model.lower():
        payload["think"] = think

    if format is not None:
        payload["format"] = format

    url = _ollama_url()
    async with httpx.AsyncClient(timeout=300.0) as client:
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
            eval_count,
            done_reason,
        )
        if done_reason == "length" and not content.strip():
            logger.warning(
                "Thinking exhausted token budget (eval=%d, num_predict=%d)",
                eval_count,
                payload["options"]["num_predict"],
            )
        return content


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
        "options": {
            "temperature": effective_temp,
            "num_predict": max_tokens,
            "top_p": 0.95,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "num_gpu": 999,
        },
    }

    url = _ollama_url()
    async with httpx.AsyncClient(timeout=300.0) as client:
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
    messages.append({
        "role": "user",
        "content": prompt,
        "images": [image_base64],
    })

    payload = {
        "model": "llama3.2-vision:11b",
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_gpu": 999,
        },
    }

    url = _ollama_url()
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        content: str = data["message"]["content"]
        eval_count = data.get("eval_count", 0)
        logger.info("analyze_image: %d content chars, %d eval tokens", len(content), eval_count)
        return content


async def get_embedding_async(text: str, model: str | None = None) -> list[float]:
    """Get embedding vector via Ollama (async version)."""
    payload = {
        "model": model or _embedding_model(),
        "input": text,
    }
    url = _ollama_url()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{url}/api/embed", json=payload)
        resp.raise_for_status()
        embeddings: list[float] = resp.json()["embeddings"][0]
        return embeddings


async def check_model_available(model: str | None = None) -> bool:
    """Check if a model is available in Ollama."""
    try:
        url = _ollama_url()
        async with httpx.AsyncClient(timeout=5.0) as client:
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
        async with httpx.AsyncClient(timeout=5.0) as client:
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

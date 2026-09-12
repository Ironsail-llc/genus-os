"""`check_model_available` decides the orchestrator's readiness. It has to be exact.

`startswith(target.split(":")[0])` let `qwen3-embedding:0.6b` satisfy a request
for `qwen3:8b` -- and the embedding model is exactly what `genus init`'s models
step pulls. So an instance with no chat model at all reported `/ready` 200 with
a generation model, and every agent run then failed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from robothor.llm import ollama

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pytest


def _serving(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": name} for name in names]})

    monkeypatch.setattr(ollama, "_transport", httpx.MockTransport(handler))
    monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://mock-ollama")


def _available(model: str | None = None) -> bool:
    return asyncio.run(ollama.check_model_available(model))


def test_only_the_embedding_model_is_not_a_generation_model(monkeypatch) -> None:
    """The shape `genus init` actually produces: it pulls the embedder and the
    reranker, and no chat model."""
    _serving(monkeypatch, "qwen3-embedding:0.6b", "Qwen3-Reranker-0.6B:F16")

    assert _available("qwen3:8b") is False


def test_a_reranker_is_not_a_generation_model_either(monkeypatch) -> None:
    _serving(monkeypatch, "Qwen3-Reranker-0.6B:F16")

    assert _available("Qwen3-Reranker-0.6B:F16") is False


def test_the_exact_model_counts(monkeypatch) -> None:
    _serving(monkeypatch, "qwen3-embedding:0.6b", "qwen3:8b")

    assert _available("qwen3:8b") is True


def test_another_tag_of_the_same_model_counts(monkeypatch) -> None:
    """A tag is a size, not a different model: an operator who pulled
    `qwen3:32b` has the generation model `qwen3:8b` asked for."""
    _serving(monkeypatch, "qwen3:32b")

    assert _available("qwen3:8b") is True


def test_a_bare_name_matches_any_tag(monkeypatch) -> None:
    _serving(monkeypatch, "llama3.2:3b")

    assert _available("llama3.2") is True


def test_a_different_model_does_not_count(monkeypatch) -> None:
    _serving(monkeypatch, "llama3.2:3b")

    assert _available("qwen3:8b") is False


def test_an_unreachable_ollama_is_not_available(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ollama, "_transport", httpx.MockTransport(handler))
    monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://mock-ollama")

    assert _available("qwen3:8b") is False

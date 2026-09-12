"""The mock is only useful if the PLATFORM's own clients are satisfied by it.

Asserting the mock's JSON against itself would prove nothing — the failure mode
that matters is a mock whose shape is plausible and wrong, which passes every
HTTP assertion in CI and then lets a real install break on the first embedding
insert. So every test here calls a function the product calls at boot.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from tests.acceptance.mock_llm.app import CHAT_MODEL, EMBEDDING_DIMENSIONS, REPLY, app

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def ollama_url(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point `robothor.llm.ollama` at the mock through its own transport seam."""
    from robothor.llm import ollama

    monkeypatch.setattr(ollama, "_transport", httpx.ASGITransport(app=app))
    monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", "http://mock-llm")
    return "http://mock-llm"


@pytest.fixture(scope="module")
def served() -> Iterator[str]:
    """The mock on a real socket, for clients that do not take a transport."""
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the mock model server did not come up"

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def test_the_orchestrator_generation_check_passes(ollama_url: str) -> None:
    """`/ready` on the orchestrator calls exactly this, and a fresh box fails it."""
    from robothor.llm.ollama import check_model_available

    assert await check_model_available() is True


async def test_embeddings_are_1024_dimensional_and_deterministic(ollama_url: str) -> None:
    """`infra/migrations/001_init.sql` declares `embedding vector(1024)`."""
    from robothor.llm.ollama import get_embedding_async

    first = await get_embedding_async("the project uses pgvector")
    second = await get_embedding_async("the project uses pgvector")
    other = await get_embedding_async("something else entirely")

    assert len(first) == EMBEDDING_DIMENSIONS
    assert first == second
    assert first != other
    assert any(value != 0.0 for value in first), "a zero vector's cosine distance is NaN"


async def test_batch_embeddings_return_one_vector_per_text(ollama_url: str) -> None:
    from robothor.llm.ollama import get_embeddings_batch_async

    vectors = await get_embeddings_batch_async(["alpha", "beta", "gamma"])

    assert [len(vector) for vector in vectors] == [EMBEDDING_DIMENSIONS] * 3


async def test_local_chat_answers(ollama_url: str) -> None:
    from robothor.llm.ollama import chat

    assert REPLY in await chat([{"role": "user", "content": "say pong"}])


def test_the_init_wizard_sees_a_tool_capable_local_model(
    served: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`detect_ollama_tool_models` reads /api/tags then /api/show for capabilities.

    An instance whose local model cannot call tools has agents that cannot do
    anything, so `genus init` asks Ollama rather than assuming.
    """
    from robothor.init.context import InitContext
    from robothor.init.provider_probe import detect_ollama_tool_models

    monkeypatch.setenv("ROBOTHOR_OLLAMA_URL", served)
    ctx = InitContext(workspace=tmp_path)

    assert CHAT_MODEL in detect_ollama_tool_models(ctx)


def test_the_provider_probe_completes_against_the_mock(
    served: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`genus init`'s provider step: one real token through litellm, no cloud."""
    from robothor.init.provider_probe import probe_model

    monkeypatch.setenv("OPENROUTER_API_BASE", f"{served}/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "mock")

    result = probe_model("openrouter/openai/gpt-5.4", api_key="mock", timeout=30)

    assert result.ok, result.detail
    assert result.probed is True


def test_llm_call_returns_the_deterministic_sentence(
    served: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from robothor.engine.llm_client import llm_call

    monkeypatch.setenv("OPENROUTER_API_BASE", f"{served}/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "mock")

    response = asyncio.run(
        llm_call(
            [{"role": "user", "content": "say pong"}],
            model="openrouter/openai/gpt-5.4",
            timeout=30,
            api_key="mock",
        )
    )

    assert response.choices[0].message.content == REPLY


def test_tool_calls_are_echoed_as_a_fixed_noop(served: str) -> None:
    """Supplying tools must produce a tool call, or the engine loop never branches."""
    body = httpx.post(
        f"{served}/v1/chat/completions",
        json={
            "model": CHAT_MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "noop", "parameters": {}}}],
        },
        timeout=30,
    ).json()

    call = body["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "noop"
    assert body["choices"][0]["finish_reason"] == "tool_calls"


def test_streaming_completions_terminate(served: str) -> None:
    with httpx.stream(
        "POST",
        f"{served}/v1/chat/completions",
        json={
            "model": CHAT_MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        timeout=30,
    ) as response:
        chunks = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert chunks[-1] == "data: [DONE]"
    assert REPLY in chunks[0]


def test_models_endpoint_lists_the_chat_model(served: str) -> None:
    body = httpx.get(f"{served}/v1/models", timeout=30).json()

    assert CHAT_MODEL in [entry["id"] for entry in body["data"]]

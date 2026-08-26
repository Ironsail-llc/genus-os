"""The offline tier must not remove itself under the load it exists to absorb.

Every agent's chain now ends in one local Ollama model. That server runs
`OLLAMA_NUM_PARALLEL=2` with a queue of 8, and answers 503 once the queue is
full. A 503 is in the mark-broken list, so during a cloud outage — the only
time the whole fleet falls through to it at once — the tier marks itself
broken, opens the circuit breaker for 600s, and the fallback that exists for
outages is unavailable during outages.

Queue-full is backpressure, not death. It is the one failure that says
"try again shortly" most literally.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker

LOCAL = "ollama_chat/qwen3.8:27b"


@pytest.fixture
def client() -> LLMClient:
    return LLMClient()


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch):
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    monkeypatch.setattr(llm_client, "LOCAL_CAPACITY_RETRY_JITTER", 0.0)


def _busy() -> Exception:
    e = Exception("server busy")
    e.status_code = 503
    return e


async def _run(client, acompletion, chain, broken=None):
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        return await client._call_llm(
            [{"role": "user", "content": "hi"}],
            list(chain),
            [],
            broken_models=broken if broken is not None else set(),
        )


@pytest.mark.asyncio
async def test_a_queue_full_local_tier_is_not_marked_broken(client):
    """Otherwise the next model in the next run skips the only tier that works."""
    broken: set[str] = set()
    acompletion = AsyncMock(side_effect=_busy())

    await _run(client, acompletion, (LOCAL,), broken=broken)

    assert LOCAL not in broken, "backpressure is not a dead model"


@pytest.mark.asyncio
async def test_a_queue_full_local_tier_does_not_open_the_circuit_breaker(client, monkeypatch):
    """Three busy responses must not blind the whole fleet for 600s."""
    breaker = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: breaker)
    acompletion = AsyncMock(side_effect=_busy())

    for _ in range(3):
        await _run(client, acompletion, (LOCAL,))

    assert not breaker.is_open(LOCAL)


@pytest.mark.asyncio
async def test_the_local_tier_gets_more_patience_than_a_cloud_model(client):
    """A drained queue answers in seconds; one retry throws that away."""
    ok = object()
    acompletion = AsyncMock(side_effect=[_busy(), _busy(), _busy(), ok])

    result = await _run(client, acompletion, (LOCAL,))

    assert result is ok
    assert acompletion.call_count == 4


@pytest.mark.asyncio
async def test_a_real_local_failure_is_still_marked_broken(client):
    """The exemption is for backpressure only — a 500 is still a dead server."""
    broken: set[str] = set()
    err = Exception("internal error")
    err.status_code = 500
    acompletion = AsyncMock(side_effect=err)

    await _run(client, acompletion, (LOCAL,), broken=broken)

    assert LOCAL in broken


@pytest.mark.asyncio
async def test_a_cloud_model_503_is_still_marked_broken(client):
    """Only the keyless local tier gets this exemption."""
    broken: set[str] = set()
    acompletion = AsyncMock(side_effect=_busy())

    await _run(client, acompletion, ("openrouter/primary",), broken=broken)

    assert "openrouter/primary" in broken


@pytest.mark.asyncio
async def test_a_successful_stream_clears_the_circuit_breaker(monkeypatch):
    """Chat streams. If success never reaches the breaker, only failures do.

    A breaker opened by cron failures then stays open against a provider that
    is demonstrably answering the operator's own messages, because the path
    that proves it healthy is the one path that never says so.
    """
    breaker = ModelBreaker(on_open=None)
    recorded: list[str] = []
    original = breaker.record_success
    breaker.record_success = lambda m: (recorded.append(m), original(m))[1]  # type: ignore[method-assign]
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: breaker)
    client = LLMClient()
    model = "openrouter/primary"

    class _Delta:
        content = "hello"
        tool_calls = None
        reasoning_content = None

    class _Choice:
        delta = _Delta()
        finish_reason = None

    class _Chunk:
        choices = [_Choice()]
        usage = None

    class _Stream:
        def __aiter__(self):
            async def gen():
                yield _Chunk()

            return gen()

    sentinel = object()
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", AsyncMock(return_value=_Stream())),
        patch("robothor.engine.llm_client.litellm.stream_chunk_builder", return_value=sentinel),
    ):
        result = await client._call_llm_streaming(
            [{"role": "user", "content": "hi"}], [model], [], broken_models=set()
        )

    assert result is sentinel
    assert recorded == [model], (
        "a fresh breaker is closed anyway — this asserts the call actually happened"
    )

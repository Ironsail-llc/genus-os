"""A fallback request carries nothing that only the primary's family accepts.

A run ended "All models failed": the primary answered 403, and the fallback, a
model of another provider family, rejected the request it was sent as a bad
request. The request is rebuilt per model, and its parameters were clean; the
history was not. An assistant turn the primary had answered earlier with
content blocks (an Anthropic-style ``thinking`` block, with its signature,
before the text) was stored verbatim and replayed to every model in the chain.
Only the family that produced a thinking block accepts one.

These tests drive the real recorder for the first turn and the real dispatch
for the second, with the provider mocked, and read what the fallback was sent.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker
from robothor.engine.run_llm_calls import LLMCallMixin
from robothor.engine.session import AgentSession

PRIMARY = "anthropic/claude-sonnet-4-6"
THINKING_BLOCK = {"type": "thinking", "thinking": "weighing the options", "signature": "sig-abc"}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "example_tool",
            "description": "An example.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _response(content: Any, model: str) -> Any:
    message = SimpleNamespace(content=content, tool_calls=None)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, completion_tokens_details=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage, model=model
    )


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    fresh = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)


class _Runner(LLMCallMixin):
    def __init__(self) -> None:
        self._llm = LLMClient()
        self.config = SimpleNamespace()

    @property
    def _active_watchdog(self) -> Any:
        return None

    def _response_cost(self, **_kwargs: Any) -> float:
        return 0.0


async def _second_turn_requests(fallback: str) -> list[dict[str, Any]]:
    """Turn one answered by the primary with a thinking block; turn two: 403, fallback."""
    runner = _Runner()
    session = AgentSession(agent_id="example-agent")
    session.messages = [
        {"role": "system", "content": "You are example-agent."},
        {"role": "user", "content": "first question"},
    ]
    first = _response([THINKING_BLOCK, {"type": "text", "text": "first answer"}], PRIMARY)
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=AsyncMock(return_value=first)),
    ):
        await runner._llm_call_and_record(session, [PRIMARY, fallback], TOOLS, None, set(), 0.3)
    if session.messages[-1].get("role") != "assistant":
        raise AssertionError("the recorder no longer appends the assistant turn")
    session.messages.append({"role": "user", "content": "second question"})

    sent: list[dict[str, Any]] = []

    async def _provider(**kwargs: Any) -> Any:
        # A snapshot: the payload's list is the session's, which grows after.
        sent.append(json.loads(json.dumps(kwargs, default=str)))
        if kwargs["model"] == PRIMARY:
            err = Exception("403 Forbidden")
            err.status_code = 403  # type: ignore[attr-defined]
            raise err
        return _response("second answer", fallback)

    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=_provider),
    ):
        await runner._llm_call_and_record(session, [PRIMARY, fallback], TOOLS, None, set(), 0.3)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["openai/gpt-6-luna", "vertex_ai/gemini-3.8-flash"])
async def test_the_fallback_is_sent_nothing_only_the_primary_accepts(fallback: str) -> None:
    sent = await _second_turn_requests(fallback)
    to_fallback = [k for k in sent if k["model"] == fallback]
    assert to_fallback, "the fallback was never called"
    request = to_fallback[-1]
    body = json.dumps(request["messages"], default=str)
    assert '"thinking"' not in body
    assert "sig-abc" not in body
    assert "cache_control" not in body
    assert "cache_control" not in json.dumps(request.get("tools") or [], default=str)
    assert "thinking" not in request
    assert "extra_body" not in request
    assert not any(key.startswith("_") for m in request["messages"] for key in m)
    # The turn's own words survive.
    assert "first answer" in body


@pytest.mark.asyncio
async def test_the_primary_still_gets_its_own_thinking_block_back() -> None:
    sent = await _second_turn_requests("openai/gpt-6-luna")
    to_primary = [k for k in sent if k["model"] == PRIMARY][-1]
    assert "sig-abc" in json.dumps(to_primary["messages"], default=str)


def test_an_untagged_thinking_block_is_kept_only_for_its_family() -> None:
    """History stored before producers were tagged: judged by the target."""
    from robothor.engine.reasoning_replay import strip_reasoning_for_model

    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [THINKING_BLOCK, {"type": "text", "text": "a"}]},
    ]
    other = strip_reasoning_for_model(history, "vertex_ai/gemini-3.8-flash")
    assert "sig-abc" not in json.dumps(other)
    assert "a" in json.dumps(other)
    same_family = strip_reasoning_for_model(history, PRIMARY)
    assert "sig-abc" in json.dumps(same_family)
    assert history[1]["content"][0] == THINKING_BLOCK, "the stored history is never mutated"

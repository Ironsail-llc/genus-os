"""Native prompt boundaries and reasoning/output parameters survive both SDKs."""

import json
import os
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("langchain_openai")

from bench.runtime.candidates import FixtureGateway
from bench.runtime.live_candidates import screening_provider
from bench.runtime.native_profile import candidate_from_native


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["pydantic-ai", "deepagents"])
@pytest.mark.parametrize("with_history", [False, True])
async def test_native_initial_configuration_survives_candidate_transport(runtime, with_history):
    artifact = os.environ.get("ROBOTHOR_RUNTIME_NATIVE_WIRE_ARTIFACT")
    payload = (
        json.loads(Path(artifact).read_text())
        if artifact
        else {
            "model": "deepseek/deepseek-v4.1-flash",
            "messages": [
                {"role": "system", "content": "Host system prompt"},
                {"role": "system", "content": "Host authority context"},
                {"role": "user", "content": "Store report=delivered"},
            ],
            "tools": FixtureGateway("fixture").schemas,
            "temperature": 0.2,
            "max_tokens": 16384,
            "thinking": {"type": "enabled", "budget_tokens": 8192},
            "tool_choice": "auto",
            "usage": {"include": True},
        }
    )
    if with_history:
        payload["messages"][-1:-1] = [
            {"role": "user", "content": "Remember the report destination is delivered."},
            {"role": "assistant", "content": "Understood; no action taken yet."},
            {"role": "user", "content": "Keep the key report."},
            {"role": "assistant", "content": "The key remains report."},
        ]
    original = deepcopy(payload)
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "type": "function",
                                    "function": {
                                        "name": "record",
                                        "arguments": '{"key":"report","value":"delivered"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = screening_provider("synthetic-key", http_client=client)
        candidate, prompt = candidate_from_native(runtime, payload, provider, client)
        result = await candidate.run(FixtureGateway("fixture"), tenant="fixture", prompt=prompt)
    assert payload == original
    assert result["verified"] and len(captured) == 1
    # The Chat Completions API defines omitted stream as false; preserve every other field.
    assert captured[0] == {**payload, "stream": False}


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "user", "content": "unfinished"}],
        [{"role": "assistant", "content": "wrong order"}, {"role": "user", "content": "x"}],
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "x", "tool_calls": []}],
        [{"role": "tool", "content": "x"}, {"role": "assistant", "content": "x"}],
    ],
)
def test_unsupported_history_refused_before_execution(history):
    from bench.runtime.conversation import text_history

    with pytest.raises(ValueError, match="complete user/assistant text pairs"):
        text_history(history)

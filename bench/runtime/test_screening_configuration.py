"""Compare actual provider payloads, not merely the intended adapter configuration."""

import json

import httpx
import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("langchain_openai")

from bench.runtime.candidates import PROMPT, SYSTEM, FixtureGateway
from bench.runtime.live_candidates import screening_candidate, screening_provider
from bench.runtime.provider_capture import ProviderCapture


@pytest.mark.asyncio
@pytest.mark.parametrize("temperature", [0.2, 0.9])
async def test_controlled_candidates_send_identical_initial_provider_requests(temperature):
    requests, traces = {}, {}
    capture = ProviderCapture()
    for runtime in ("pydantic-ai", "deepagents"):
        captured = []

        def respond(request, captured=captured):
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "synthetic",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "synthetic-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "tool",
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

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), event_hooks={"request": [capture.record]}
        ) as client:
            provider = screening_provider("synthetic-key", http_client=client)
            adapter = screening_candidate(
                runtime,
                "synthetic-model",
                {"temperature": temperature},
                "synthetic-key",
                provider,
                http_client=client,
            )
            token = capture.start()
            try:
                result = await adapter.run(FixtureGateway("fixture"), tenant="fixture")
            finally:
                traces[runtime] = capture.finish(token)
            assert result["verified"] and result["model_calls"] == 1
        assert len(captured) == 1
        requests[runtime] = captured[0]
    assert requests["pydantic-ai"] == requests["deepagents"]
    assert traces["pydantic-ai"] == traces["deepagents"]
    assert len(traces["pydantic-ai"]) == 1
    assert "synthetic-key" not in json.dumps(traces)
    payload = requests["pydantic-ai"]
    assert payload["temperature"] == temperature
    assert payload["max_completion_tokens"] == 512
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["strict"] is True
    assert payload["messages"] == [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": PROMPT},
    ]

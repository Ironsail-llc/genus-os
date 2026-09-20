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


@pytest.mark.asyncio
async def test_failed_screening_preserves_unknown_usage_and_observed_attempts(
    tmp_path, monkeypatch
):
    from bench.runtime.live_candidates import screen

    manifest = tmp_path / "models.yaml"
    manifest.write_text("model:\n  primary: openrouter/synthetic-model\n  temperature: 0.2\n")
    output = tmp_path / "failures.jsonl"
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    original_init = httpx.AsyncClient.__init__
    requests = []

    def fail(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "synthetic provider failure"}})

    def mocked_client(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fail)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mocked_client)
    report = await screen(manifest, output, 1)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == len(requests) == 2
    assert all(scenario["failures"] == 1 for scenario in report["scenarios"].values())
    for row in rows:
        assert row["status"] == "failed" and row["writes"] == row["dispatches"] == 0
        assert row["model_calls"] == 1 and row["framework_model_calls"] is None
        assert row["input_tokens"] is row["output_tokens"] is row["cost_usd"] is None
        assert row["duration_ms"] == row["execution_ms"] + row["queue_ms"]
        assert row["queue_ms"] >= 0
        assert len(row["provider_requests"]) == 1
        assert row["provider_requests"][0]["model_settings"]["temperature"] == 0.2
    assert "synthetic-test-key" not in output.read_text()

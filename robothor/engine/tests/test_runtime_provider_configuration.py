"""Observe the native loop's HTTP payload with no provider/network execution."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from bench.runtime.candidates import PROMPT, FixtureGateway
from bench.runtime.provider_capture import ProviderCapture
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.workflow_completion import WorkflowCompletion, workflow_completion_scope


@pytest.mark.usefixtures("_mock_run_persistence")
async def test_native_provider_configuration_is_observed_at_http_boundary(
    request, sample_agent_config, monkeypatch
):
    engine = request.getfixturevalue("runner")
    gateway, capture, payloads = FixtureGateway("fixture"), ProviderCapture(), []
    sample_agent_config.model_primary = "openrouter/deepseek/deepseek-v4.1-flash"
    sample_agent_config.model_fallbacks = []
    sample_agent_config.temperature = 0.2
    sample_agent_config.task_protocol = False
    sample_agent_config.max_iterations = 4
    sample_agent_config.tools_allowed = ["record"]
    engine.registry.build_for_agent.return_value = gateway.schemas
    engine.registry.get_tool_names.return_value = ["record"]

    async def dispatch(name, args, **kwargs):
        return await gateway.invoke("fixture", name, args)

    engine.registry.execute = AsyncMock(side_effect=dispatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")

    async def respond(client, req, **kwargs):
        assert req.url.host == "openrouter.ai", "unexpected outbound request denied"
        assert req.url.path.endswith("/chat/completions")
        await req.aread()
        await capture.record(req)
        payloads.append(json.loads(req.content))
        return httpx.Response(
            200,
            request=req,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek/deepseek-v4.1-flash",
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

    monkeypatch.setattr(httpx.AsyncClient, "send", respond)
    context = ExecutionContext(
        engine.config.tenant_id,
        "synthetic-operator",
        "synthetic-request",
        deadline=datetime.now(UTC) + timedelta(seconds=5),
    )
    token = capture.start()
    try:
        with workflow_completion_scope(
            context.tenant_id,
            "test-agent",
            lambda: WorkflowCompletion(output="Verified") if gateway.verified else None,
        ):
            result = await CurrentRuntime(engine.execute).run(
                RunRequest(
                    context, "test-agent", PROMPT, options={"agent_config": sample_agent_config}
                )
            )
    finally:
        observed = capture.finish(token)
    assert str(result.run.status) == "completed", result.run.error_message
    assert gateway.verified and len(payloads) == len(observed) == 1
    payload = payloads[0]
    assert payload["temperature"] == 0.2
    assert observed[0]["model"] == "deepseek/deepseek-v4.1-flash"
    assert observed[0]["model_settings"]["temperature"] == 0.2
    # Capture is usable without retaining model prompts, tool schemas or credentials.
    assert "synthetic-test-key" not in json.dumps(observed)
    artifact = os.environ.get("ROBOTHOR_RUNTIME_NATIVE_WIRE_ARTIFACT")
    if artifact:
        with Path(artifact).open("x") as file:
            json.dump(payload, file, indent=2)

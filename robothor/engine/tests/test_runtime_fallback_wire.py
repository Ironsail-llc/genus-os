"""Native fallback uses the selected cloud chain without network or repeated writes."""

import asyncio
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.deadlines import RuntimeDeadlineError
from robothor.engine.tests.runtime_fixtures import PROMPT, FixtureGateway
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.workflow_completion import WorkflowCompletion, workflow_completion_scope

# Snapshot of main.yaml's selected cloud chain, verified locally on 2026-09-20.
# The local final fallback is not dispatched by these cloud recovery scenarios.
MODELS = [
    "openrouter/deepseek/deepseek-v4.1-flash",
    "openrouter/xiaomi/mimo-v2.5",
    "openrouter/deepseek/deepseek-v4-flash",
    "ollama_chat/qwen3.8:27b",
]


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.parametrize("unavailable", [1, 2])
@pytest.mark.parametrize("expire", [False, True])
async def test_native_fallback_wire_preserves_deadline_and_single_effect(
    request, sample_agent_config, monkeypatch, unavailable, expire
):
    engine = request.getfixturevalue("runner")
    persisted = Mock()
    monkeypatch.setattr(engine, "_persist_run_sync", persisted)
    gateway, payloads = FixtureGateway("fixture"), []
    sample_agent_config.model_primary = MODELS[0]
    sample_agent_config.model_fallbacks = MODELS[1:]
    sample_agent_config.temperature = 0.5
    sample_agent_config.task_protocol = False
    sample_agent_config.tools_allowed = ["record"]
    engine.registry.build_for_agent.return_value = gateway.schemas
    engine.registry.get_tool_names.return_value = ["record"]

    async def dispatch(name, args, **kwargs):
        return await gateway.invoke("fixture", name, args)

    engine.registry.execute = AsyncMock(side_effect=dispatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    # Keep the configured retry count while removing jitter from this contract.
    monkeypatch.setattr("robothor.engine.llm_client.TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr("robothor.engine.llm_client.TRANSIENT_RETRY_JITTER_MAX", 0.0)
    deadline = datetime.now(UTC) + timedelta(seconds=0.5 if expire else 10)
    context = ExecutionContext(
        engine.config.tenant_id, "fixture-owner", "fixture-request", deadline=deadline
    )

    async def respond(client, req, **kwargs):
        assert req.url.host == "openrouter.ai" and req.url.path.endswith("/chat/completions"), (
            "Unexpected outbound request denied"
        )
        assert not gateway.verified, "No provider call after verified success"
        assert active_context.get().deadline == deadline
        await req.aread()
        payload = json.loads(req.content)
        payloads.append(payload)
        if payload["model"] in [name.removeprefix("openrouter/") for name in MODELS[:unavailable]]:
            return httpx.Response(
                503, request=req, json={"error": {"message": "Synthetic outage", "code": 503}}
            )
        if expire:
            await asyncio.Event().wait()
        return httpx.Response(
            200,
            request=req,
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
                                    "id": "one-write",
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
    with (
        pytest.raises(RuntimeDeadlineError, match="execution cancelled")
        if expire
        else nullcontext(),
        workflow_completion_scope(
            context.tenant_id,
            "test-agent",
            lambda: WorkflowCompletion(output="Stored and verified.") if gateway.verified else None,
        ),
    ):
        result = await CurrentRuntime(engine.execute).run(
            RunRequest(context, "test-agent", PROMPT, options={"agent_config": sample_agent_config})
        )
    expected = [name.removeprefix("openrouter/") for name in MODELS[:unavailable] for _ in range(2)]
    expected.append(MODELS[unavailable].removeprefix("openrouter/"))
    assert [payload["model"] for payload in payloads] == expected
    assert all(payload["temperature"] == 0.5 for payload in payloads)
    if expire:
        recorded = persisted.call_args.args[0]
        assert str(recorded.status) == "cancelled"
        assert "Runtime deadline expired" in recorded.error_message
        assert gateway.writes == 0 and not gateway.verified
        engine.registry.execute.assert_not_awaited()
    else:
        assert str(result.run.status) == "completed", result.run.error_message
        assert gateway.verified and gateway.writes == 1
        assert result.run.output_text == "Stored and verified."
        engine.registry.execute.assert_awaited_once()

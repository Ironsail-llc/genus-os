"""Exercise trusted action acceptance through the actual native runtime loop."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.output_validation import output_validation_scope
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.workflow_completion import WorkflowCompletion, workflow_completion_scope


async def test_synthetic_gateway_rejects_unrequested_writes_and_deduplicates():
    from bench.runtime.candidates import FixtureGateway

    gateway = FixtureGateway("fixture")
    assert (await gateway.dispatch("fixture")).get("error")
    assert (await gateway.dispatch("fixture", key="report")).get("error")
    assert (
        await gateway.dispatch("fixture", key="report", value="delivered", unrelated="argument")
    ).get("error")
    denied = await gateway.dispatch("fixture", key="unrequested", value="delivered")
    assert denied.get("error")
    assert gateway.values == {} and gateway.writes == 0
    for _ in range(2):
        result = await gateway.dispatch("fixture", key="report", value="delivered")
        assert result["verification"] == "verified"
    assert gateway.writes == 1


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.parametrize(
    "perpetual_clarification,missing_arguments", [(False, False), (False, True), (True, False)]
)
async def test_native_verified_action_repairs_or_fails_without_false_completion(
    request,
    sample_agent_config,
    mock_litellm_response,
    monkeypatch,
    perpetual_clarification,
    missing_arguments,
):
    import litellm

    from bench.runtime.candidates import FixtureGateway

    engine = request.getfixturevalue("runner")
    monkeypatch.setattr(engine, "_persist_run_sync", MagicMock())
    sample_agent_config.task_protocol = False
    sample_agent_config.max_iterations = 4
    sample_agent_config.tools_allowed = ["record"]
    engine.registry.build_for_agent.return_value = [
        {"type": "function", "function": {"name": "record"}}
    ]
    engine.registry.get_tool_names.return_value = ["record"]
    writes = []
    gateway = FixtureGateway("fixture")

    async def dispatch(name, args, **kwargs):
        assert name == "record"
        result = await gateway.dispatch("fixture", **args)
        if result.get("error"):
            return result
        writes.append(args)
        return result

    engine.registry.execute = AsyncMock(side_effect=dispatch)
    calls = []

    async def provider(**kwargs):
        assert not writes, "No provider work after independently verified success"
        calls.append(kwargs)
        if (len(calls) == 1 and not missing_arguments) or perpetual_clarification:
            return mock_litellm_response(content="Which key and value should I store?")
        tool = MagicMock()
        tool.id = "authorized-write"
        tool.function.name = "record"
        tool.function.arguments = '{"key":"report","value":"delivered"}'
        if len(calls) == 1:
            tool.function.arguments = "{}"
        response = mock_litellm_response(content=None, tool_calls=[tool])
        response.choices[0].message.content = None
        return response

    monkeypatch.setattr(litellm, "acompletion", provider)

    def verified():
        return writes == [{"key": "report", "value": "delivered"}]

    context = ExecutionContext(
        engine.config.tenant_id,
        "fixture-owner",
        "fixture-request",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    with (
        workflow_completion_scope(
            context.tenant_id,
            "test-agent",
            lambda: WorkflowCompletion(output="Stored and verified.") if verified() else None,
        ),
        output_validation_scope(lambda run, text: None if verified() else "Store report=delivered"),
    ):
        result = await CurrentRuntime(engine.execute).run(
            RunRequest(
                context,
                "test-agent",
                "Store key report with value delivered exactly once.",
                options={"agent_config": sample_agent_config},
            )
        )
    if perpetual_clarification:
        assert result.run.status == "failed"
        assert "validation failed" in result.run.error_message
        assert len(calls) == 3
        assert writes == []
    else:
        assert result.run.status == "completed"
        assert result.run.output_text == "Stored and verified."
        assert len(calls) == 2
        assert verified()

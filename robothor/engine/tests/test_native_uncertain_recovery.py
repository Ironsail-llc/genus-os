"""Native recovery observes an uncertain write and stops at verified completion."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.output_validation import output_validation_scope
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.workflow_completion import WorkflowCompletion, workflow_completion_scope


@pytest.mark.usefixtures("_mock_run_persistence")
async def test_native_uncertain_write_uses_readback_then_stops(
    request, sample_agent_config, mock_litellm_response, monkeypatch
):
    engine = request.getfixturevalue("runner")
    sample_agent_config.task_protocol = False
    sample_agent_config.max_iterations = 4
    sample_agent_config.tools_allowed = ["create_note", "get_note"]
    engine.registry.build_for_agent.return_value = [
        {"type": "function", "function": {"name": name}}
        for name in sample_agent_config.tools_allowed
    ]
    engine.registry.get_tool_names.return_value = sample_agent_config.tools_allowed
    effects, calls, observed = [], [], []

    async def dispatch(name, args, **kwargs):
        calls.append(name)
        if name == "create_note":
            effects.append("saved once")
            return {
                "error": "backing service timed out",
                "retryable": False,
                "outcome_unknown": True,
            }
        assert name == "get_note"
        observed[:] = effects
        return {"notes": observed.copy()}

    engine.registry.execute = AsyncMock(side_effect=dispatch)
    provider_calls = []

    async def provider(**kwargs):
        assert not observed, "No model work after independent readback"
        provider_calls.append(kwargs)
        if calls:
            text = "\n".join(str(message.get("content", "")) for message in kwargs["messages"])
            assert "read-only" in text and "original audit" in text
            assert "Do not repeat" in text and "retrying once" not in text
        tool = MagicMock()
        tool.id = f"call-{len(provider_calls)}"
        tool.function.name = "get_note" if calls else "create_note"
        tool.function.arguments = "{}"
        response = mock_litellm_response(content=None, tool_calls=[tool])
        response.choices[0].message.content = None
        return response

    monkeypatch.setattr("litellm.acompletion", provider)
    context = ExecutionContext(
        engine.config.tenant_id,
        "fixture-owner",
        "fixture-uncertain-write",
        deadline=datetime.now(UTC) + timedelta(seconds=20),
    )

    def verified():
        return observed == ["saved once"] and effects == ["saved once"]

    with (
        workflow_completion_scope(
            context.tenant_id,
            "test-agent",
            lambda: (
                WorkflowCompletion(output="Saved once; confirmed by readback.")
                if verified()
                else None
            ),
        ),
        output_validation_scope(lambda run, text: None if verified() else "Missing readback"),
    ):
        result = await CurrentRuntime(engine.execute).run(
            RunRequest(
                context,
                "test-agent",
                "Save the note once and verify it.",
                options={"agent_config": sample_agent_config},
            )
        )
    assert result.run.status == "completed"
    assert result.run.output_text == "Saved once; confirmed by readback."
    assert calls == ["create_note", "get_note"] and verified()
    assert len(provider_calls) == 2

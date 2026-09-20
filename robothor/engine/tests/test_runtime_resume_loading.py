"""A checkpoint disappearing after admission must never become fresh execution."""

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.run_lifecycle import RunLifecycleMixin
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.contracts import StateEnvelope
from robothor.engine.session import AgentSession
from robothor.engine.tests.test_runner import runner  # noqa: F401


@pytest.mark.parametrize("reload", [None, {"messages": []}, {"messages": "invalid"}])
async def test_checkpoint_reload_failure_cannot_fall_through_to_action(reload):
    session = AgentSession("main", tenant_id="fixture")
    action = AsyncMock()
    checkpoint = {"messages": [{"role": "user", "content": "Already started"}]}

    async def execute(**options):
        RunLifecycleMixin()._resume_from_checkpoint(options["resume_from_run_id"], session)
        await action()
        return AgentRun(status=RunStatus.COMPLETED)

    request = RunRequest(
        ExecutionContext("fixture", "operator", "request"),
        "main",
        "Original action",
        resume_from="previous",
        checkpoint=StateEnvelope(),
    )
    with (
        patch(
            "robothor.engine.checkpoint.CheckpointManager.load_latest",
            side_effect=[checkpoint, reload],
        ) as load,
        patch("robothor.engine.runtime.controls.stopped", return_value=False),
        pytest.raises(RuntimeError, match="checkpoint"),
    ):
        await CurrentRuntime(execute).run(request)
    action.assert_not_awaited()
    assert load.call_count == 2
    assert all(call.kwargs == {"tenant_id": "fixture"} for call in load.call_args_list)


def test_compatible_checkpoint_restores_messages_without_optional_scratchpad():
    session = AgentSession("main", tenant_id="fixture")
    session.originating_message = "Continue existing work"
    messages = [{"role": "user", "content": "Continue existing work"}]
    with patch(
        "robothor.engine.checkpoint.CheckpointManager.load_latest",
        return_value={"messages": messages},
    ):
        assert RunLifecycleMixin()._resume_from_checkpoint("previous", session) is None
    assert any(message.get("content") == "Continue existing work" for message in session.messages)


async def test_native_runner_records_failed_recovery_without_model_or_business_calls(
    request,
    sample_agent_config,
):
    engine = request.getfixturevalue("runner")
    engine.registry.execute = AsyncMock()
    checkpoint = {"messages": [{"role": "user", "content": "Already started"}]}
    with (
        patch(
            "robothor.engine.checkpoint.CheckpointManager.load_latest",
            side_effect=[checkpoint, None],
        ),
        patch("robothor.engine.runtime.controls.stopped", return_value=False),
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", new_callable=AsyncMock) as provider,
    ):
        result = await engine.execute(
            "test-agent",
            "Original action",
            agent_config=sample_agent_config,
            tenant_id="fixture",
            resume_from_run_id="previous",
        )
    assert result.status == RunStatus.FAILED
    assert "Failed to restore checkpoint" in result.error_message
    provider.assert_not_awaited()
    engine.registry.execute.assert_not_awaited()

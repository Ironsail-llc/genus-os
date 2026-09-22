"""Native interactive manifest resolution supplies an admission-time deadline."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import litellm
import pytest

from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.profile_admission import prepare
from robothor.engine.tests.test_runner import runner  # noqa: F401


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_native_interactive_loads_simple_profile_once_before_model(
    runner,  # noqa: F811
    sample_agent_config,
    monkeypatch,  # noqa: F811
    delivery_fails,
):
    sample_agent_config.difficulty_class = "simple"
    sample_agent_config.task_protocol = False
    loader = Mock(return_value=(sample_agent_config, ""))
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)
    admitted = datetime.now(UTC)
    events = []

    async def status(event):
        events.append(event)
        if delivery_fails and event.get("event") == "accepted":
            raise OSError("synthetic delivery loss")

    async def reply(**kwargs):
        context = active_context.get()
        assert context.deadline is not None
        assert 0 < (context.deadline - datetime.now(UTC)).total_seconds() <= 60
        assert context.deadline <= admitted + timedelta(seconds=60.1)
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {"role": "assistant", "content": "Acknowledged."},
                    "finish_reason": "stop",
                }
            ]
        )

    model = AsyncMock(side_effect=reply)
    monkeypatch.setattr("litellm.acompletion", model)
    run = await runner.execute(
        sample_agent_config.id,
        "Acknowledge this message",
        trigger_type=TriggerType.WEBCHAT,
        user_id="operator",
        user_role="owner",
        on_status=status,
    )
    assert run.status == RunStatus.COMPLETED
    loader.assert_called_once_with(sample_agent_config.id, runner.config.manifest_dir)
    model.assert_awaited_once()
    assert sum(event.get("event") == "accepted" for event in events) == 1
    assert active_context.get() is None


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.parametrize(
    "reason", ["Agent config not found: absent", "Agent manifest rejected by schema: absent"]
)
async def test_failed_profile_lookup_keeps_native_refusal_without_second_read(
    runner,  # noqa: F811
    monkeypatch,
    reason,  # noqa: F811
):
    loader = Mock(return_value=(None, reason))
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)
    model = AsyncMock(side_effect=AssertionError("Refused manifest must not call a model"))
    monkeypatch.setattr("litellm.acompletion", model)
    run = await runner.execute(
        "absent",
        "Do the work",
        trigger_type=TriggerType.WEBCHAT,
        user_id="operator",
        user_role="owner",
    )
    assert run.status == RunStatus.FAILED and run.error_message == reason
    loader.assert_called_once()
    model.assert_not_awaited()
    assert active_context.get() is None


async def test_profile_lookup_time_is_not_added_to_simple_deadline(
    sample_agent_config, monkeypatch
):
    from types import SimpleNamespace

    from robothor.engine.runtime.contracts import ExecutionContext, RunRequest

    sample_agent_config.difficulty_class = "simple"
    loader = Mock(return_value=(sample_agent_config, ""))
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)
    admitted = datetime.now(UTC) - timedelta(seconds=40)
    request = RunRequest(
        ExecutionContext("tenant", "operator", "request"),
        "main",
        "Do the work",
        {"trigger_type": "webchat"},
    )
    bounded, _ = await prepare(
        SimpleNamespace(config=SimpleNamespace(manifest_dir="fixture")), request, admitted
    )
    assert bounded.context.deadline == admitted + timedelta(seconds=60)
    assert 19 < (bounded.context.deadline - datetime.now(UTC)).total_seconds() <= 20

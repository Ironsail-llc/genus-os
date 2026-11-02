"""A run whose container cannot start is not killed.

Today an agent with `sandbox: docker` whose container cannot start is failed at
step 0 with "Blocked by sandbox_default: sandbox required but could not be
started" -- the owner included. Now the run continues, its execution plan says
where `exec` will go, and the decision is recorded.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine import sandbox_policy as sp
from robothor.engine.models import AgentConfig
from robothor.engine.runner import AgentRunner
from robothor.engine.sandbox import RuntimeStatus


def _response(content: str = "Done") -> Any:
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


def _config() -> AgentConfig:
    return AgentConfig(
        id="sb-agent",
        name="Sandboxed",
        model_primary="test-model",
        sandbox="docker",
        tools_allowed=["exec"],
    )


async def _execute(engine_config, *, runtime: RuntimeStatus, start_error: str | None, role: str):
    runner = AgentRunner(engine_config)
    events: list[dict[str, Any]] = []
    started: list[bool] = []

    async def _start(self) -> None:
        started.append(True)
        if start_error:
            raise RuntimeError(start_error)

    async def _completion(**_kwargs: Any) -> Any:
        return _response()

    with (
        patch("litellm.acompletion", side_effect=_completion),
        patch.object(runner.registry, "build_for_agent", return_value=[]),
        patch.object(runner.registry, "get_tool_names", return_value=["exec"]),
        patch("robothor.engine.sandbox.Sandbox.start", _start),
        patch("robothor.engine.sandbox.runtime_status", return_value=runtime),
        patch(
            "robothor.engine.tracking.log_guardrail_event",
            side_effect=lambda *a, **k: events.append({"args": a, **k}),
        ),
    ):
        run = await runner.execute(
            "sb-agent",
            "hello",
            agent_config=_config(),
            user_id="user-1",
            user_role=role,
        )
    return run, events, started


def _named(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("guardrail_name") == name]


ABSENT = RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker")
PRESENT = RuntimeStatus(binary="podman", available=True, reason="")


@pytest.mark.asyncio
async def test_no_runtime_owner_run_continues_on_the_host(engine_config, mock_db):
    run, events, started = await _execute(
        engine_config, runtime=ABSENT, start_error=None, role="owner"
    )
    assert run.status.value == "completed", run.error_message
    assert started == [], "no runtime: there is nothing to start"
    plan = sp.exec_plan_for(run.id)
    assert plan is not None and plan.where == "host" and plan.degraded
    recorded = _named(events, "sandbox_playground")
    assert len(recorded) == 1
    assert recorded[0]["action"] == "observed"


@pytest.mark.asyncio
async def test_no_runtime_member_run_continues_and_exec_will_refuse(engine_config, mock_db):
    run, events, _ = await _execute(engine_config, runtime=ABSENT, start_error=None, role="member")
    assert run.status.value == "completed", run.error_message
    plan = sp.exec_plan_for(run.id)
    assert plan is not None and plan.where == "refuse"
    assert len(_named(events, "sandbox_playground")) == 1


@pytest.mark.asyncio
async def test_a_start_failure_degrades_through_the_plan(engine_config, mock_db):
    run, events, started = await _execute(
        engine_config, runtime=PRESENT, start_error="uid_map write failed", role="owner"
    )
    assert started == [True]
    assert run.status.value == "completed", run.error_message
    plan = sp.exec_plan_for(run.id)
    assert plan is not None and plan.where == "host"
    assert "uid_map write failed" in plan.reason
    assert len(_named(events, "sandbox_playground")) == 1

"""The runner tells the operations ledger which run a tool call belongs to.

A tool that proposes an action (``Operations.propose``) records the run, the
person whose interactive turn it was, and the workspace, from
``robothor.operations.proposer.current_proposer()``. These drive the real
runner loop and look at what a tool sees while it executes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runner import AgentRunner

USER = str(uuid.uuid4())
ROOM = str(uuid.uuid4())


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        mock_registry = MagicMock()
        mock_registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        mock_registry.get_tool_names.return_value = ["list_tasks"]
        mock_reg.return_value = mock_registry
        r = AgentRunner(engine_config)
        r.registry = mock_registry
        yield r


async def _run_once(runner, mock_litellm_response, **execute: Any):
    from robothor.operations.proposer import current_proposer

    seen: list[Any] = []

    async def _tool(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append((current_proposer(), kwargs.get("run_id")))
        return {"ok": True}

    tc = MagicMock()
    tc.id = "c1"
    tc.function.name = "list_tasks"
    tc.function.arguments = json.dumps({})
    first = mock_litellm_response(content=None, tool_calls=[tc])
    first.choices[0].message.content = None
    done = mock_litellm_response(content="done")
    calls = {"n": 0}

    async def _completion(**kwargs: Any):
        calls["n"] += 1
        return first if calls["n"] == 1 else done

    runner.registry.execute = _tool
    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=_completion),
    ):
        run = await runner.execute("test-agent", "go", **execute)
    return run, seen


@pytest.mark.asyncio
async def test_a_webchat_turn_in_a_workspace_is_attributed_to_its_person(
    runner, sample_agent_config, mock_litellm_response
):
    from robothor.operations.proposer import Proposer, current_proposer

    run, seen = await _run_once(
        runner,
        mock_litellm_response,
        trigger_type=TriggerType.WEBCHAT,
        trigger_detail=f"webchat:agent:test-agent:room:{ROOM}",
        agent_config=sample_agent_config,
        user_id=USER,
        user_role="member",
    )

    assert run.status == RunStatus.COMPLETED
    [(proposer, run_id)] = seen
    assert proposer == Proposer(run_id=run_id, user_id=USER, room_id=ROOM)
    assert current_proposer() is None, "the attribution must not outlive the run"


@pytest.mark.asyncio
async def test_a_cron_run_is_attributed_to_nobody(
    runner, sample_agent_config, mock_litellm_response
):
    run, seen = await _run_once(
        runner,
        mock_litellm_response,
        trigger_type=TriggerType.CRON,
        trigger_detail="cron:daily",
        agent_config=sample_agent_config,
    )

    assert run.status == RunStatus.COMPLETED
    [(proposer, run_id)] = seen
    assert proposer is not None and proposer.run_id == run_id
    assert proposer.user_id is None and proposer.room_id is None

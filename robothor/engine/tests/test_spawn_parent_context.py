"""Stage 4 — parent_task_id injection into spawned child prompts.

When a heartbeat spawns a worker to advance a specific task, the worker
must see the parent task's objective + next_action, not just the task body.
Prevents the DrFirst failure mode where email-responder scheduled a meeting
even though the parent objective said "without scheduling a meeting".
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import (
    AgentConfig,
    AgentRun,
    DeliveryMode,
    RunStatus,
    SpawnContext,
)


@pytest.fixture
def spawn_context():
    return SpawnContext(
        parent_run_id=str(uuid.uuid4()),
        parent_agent_id="main",
        correlation_id=str(uuid.uuid4()),
        nesting_depth=0,
        max_nesting_depth=2,
        remaining_token_budget=100000,
        remaining_cost_budget_usd=0.50,
        parent_trace_id="abc",
        parent_span_id="def",
    )


@pytest.fixture
def child_agent_config():
    return AgentConfig(
        id="email-responder",
        name="Email Responder",
        model_primary="openrouter/test/model",
        max_iterations=15,
        timeout_seconds=300,
        delivery_mode=DeliveryMode.ANNOUNCE,
        tools_allowed=["list_tasks"],
    )


def _make_completed_run() -> AgentRun:
    return AgentRun(
        id=str(uuid.uuid4()),
        agent_id="email-responder",
        status=RunStatus.COMPLETED,
        output_text="done",
        input_tokens=100,
        output_tokens=50,
        total_cost_usd=0.001,
        duration_ms=1000,
    )


class TestSpawnParentContext:
    @pytest.mark.asyncio
    async def test_spawn_injects_parent_objective_into_child_prompt(
        self, spawn_context, child_agent_config
    ):
        """Child's message must contain --- PARENT TASK --- block with objective."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        parent_task_id = "task-drfirst-123"
        # get_task returns task_to_dict — camelCase keys
        parent_task = {
            "id": parent_task_id,
            "title": "DrFirst: confirm RxHistory pricing",
            "objective": "Confirm RxHistory pricing without scheduling a meeting",
            "nextAction": "Email April asking for a written quote by EOW",
            "nextActionAgent": "email-responder",
            "questionForOperator": None,
            "autonomyBudget": {"reversible_cap_usd": 500, "summary": "auto under $500"},
        }

        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with (
                patch(
                    "robothor.engine.config.load_agent_config",
                    return_value=child_agent_config,
                ),
                patch(
                    "robothor.crm.dal.get_task",
                    return_value=parent_task,
                ),
                patch(
                    "robothor.engine.dedup.try_acquire",
                    return_value=True,
                ),
                patch("robothor.engine.dedup.release"),
            ):
                await _handle_spawn_agent(
                    {
                        "agent_id": "email-responder",
                        "message": "Draft the reply",
                        "parent_task_id": parent_task_id,
                    },
                    agent_id="main",
                )

            call_kwargs = mock_runner.execute.call_args.kwargs
            message_sent = call_kwargs.get("message", "")

            assert "--- PARENT TASK" in message_sent
            assert parent_task_id in message_sent
            assert "Confirm RxHistory pricing without scheduling a meeting" in message_sent
            assert "Email April asking for a written quote by EOW" in message_sent
            # Must warn the child about contradicting the objective
            assert "DO NOT" in message_sent or "do not" in message_sent.lower()
            # Original message body must still be there after the header
            assert "Draft the reply" in message_sent
        finally:
            set_runner(None)  # type: ignore[arg-type]
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_without_parent_task_id_unchanged(self, spawn_context, child_agent_config):
        """No parent_task_id → message is passed through unchanged (backwards compat)."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with (
                patch(
                    "robothor.engine.config.load_agent_config",
                    return_value=child_agent_config,
                ),
                patch(
                    "robothor.engine.dedup.try_acquire",
                    return_value=True,
                ),
                patch("robothor.engine.dedup.release"),
            ):
                await _handle_spawn_agent(
                    {"agent_id": "email-responder", "message": "just do it"},
                    agent_id="main",
                )

            call_kwargs = mock_runner.execute.call_args.kwargs
            message_sent = call_kwargs.get("message", "")
            assert message_sent == "just do it"
            assert "PARENT TASK" not in message_sent
        finally:
            set_runner(None)  # type: ignore[arg-type]
            _current_spawn_context.set(None)

    @pytest.mark.asyncio
    async def test_spawn_with_missing_parent_task_passes_through(
        self, spawn_context, child_agent_config
    ):
        """parent_task_id that doesn't resolve → still spawns with original message."""
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agent,
            set_runner,
        )

        run = _make_completed_run()
        mock_runner = MagicMock()
        mock_runner.execute = AsyncMock(return_value=run)
        mock_runner.config = MagicMock()
        mock_runner.config.manifest_dir = "/tmp/agents"

        set_runner(mock_runner)
        _current_spawn_context.set(spawn_context)

        try:
            with (
                patch(
                    "robothor.engine.config.load_agent_config",
                    return_value=child_agent_config,
                ),
                patch(
                    "robothor.crm.dal.get_task",
                    return_value=None,
                ),
                patch(
                    "robothor.engine.dedup.try_acquire",
                    return_value=True,
                ),
                patch("robothor.engine.dedup.release"),
            ):
                result = await _handle_spawn_agent(
                    {
                        "agent_id": "email-responder",
                        "message": "orig",
                        "parent_task_id": "missing-task",
                    },
                    agent_id="main",
                )

            assert "error" not in result
            call_kwargs = mock_runner.execute.call_args.kwargs
            message_sent = call_kwargs.get("message", "")
            assert "PARENT TASK" not in message_sent
            assert "orig" in message_sent
        finally:
            set_runner(None)  # type: ignore[arg-type]
            _current_spawn_context.set(None)

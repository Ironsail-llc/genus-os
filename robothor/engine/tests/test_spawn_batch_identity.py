"""Parallel delegation must carry the same authenticated tool context as a single spawn."""

from unittest.mock import AsyncMock

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import spawn


@pytest.mark.asyncio
async def test_each_parallel_child_receives_the_authenticated_context(monkeypatch):
    context = ToolContext(
        agent_id="researcher",
        run_id="parent",
        tenant_id="tenant-a",
        user_id="service:researcher",
        user_role="sales_agent",
        accessible_tenant_ids=("tenant-a",),
        is_benchmark=True,
    )
    handler = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(spawn, "_handle_spawn_agent", handler)
    result = await spawn._handle_spawn_agents(
        {
            "agents": [
                {"agent_id": "research-worker", "message": "services"},
                {"agent_id": "research-worker", "message": "ownership"},
            ]
        },
        context,
    )
    assert result["completed"] == 2
    assert handler.await_count == 2
    for call in handler.await_args_list:
        assert call.kwargs["ctx"] is context
        assert call.kwargs["agent_id"] == "researcher"

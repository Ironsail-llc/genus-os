"""Agents get research/drafting tools, never human authorization verbs."""

from types import SimpleNamespace

import pytest

from robothor.engine.tools.handlers.sales import HANDLERS
from robothor.sales.tool_schemas import SALES_SCHEMAS


@pytest.mark.asyncio
async def test_tool_uses_context_tenant_and_rejects_tenant_in_arguments(monkeypatch):
    import robothor.engine.tools.handlers.sales as handlers

    calls = []

    class Service:
        def __init__(self, tenant):
            calls.append(tenant)

        def get(self, prospect_id):
            return {"id": prospect_id}

    monkeypatch.setattr(handlers, "Sales", Service)
    ctx = SimpleNamespace(tenant_id="tenant-a", is_benchmark=False, agent_id="researcher")
    result = await HANDLERS["sales_get_prospect"]({"prospect_id": "p-1"}, ctx)
    assert result == {"id": "p-1"}
    assert calls == ["tenant-a"]
    result = await HANDLERS["sales_get_prospect"](
        {"prospect_id": "p-1", "tenant_id": "tenant-b"}, ctx
    )
    assert "error" in result
    assert calls == ["tenant-a"]


@pytest.mark.asyncio
async def test_benchmark_cannot_create_prospects():
    ctx = SimpleNamespace(tenant_id="tenant-a", is_benchmark=True, agent_id="scout")
    result = await HANDLERS["sales_discover"](
        {"name": "Example", "website": "https://example.com", "source_url": "https://example.com"},
        ctx,
    )
    assert "error" in result


def test_no_approval_send_or_configuration_tool():
    assert set(HANDLERS) == set(SALES_SCHEMAS)
    assert not any(
        term in name
        for name in HANDLERS
        for term in ("approve", "send", "configure", "publish", "decide")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {"agent_id": "researcher", "user_id": "service:researcher", "user_role": "service"},
        {"agent_id": "workflow:sales-research", "user_id": "human-1", "user_role": "owner"},
        {
            "agent_id": "workflow:sales-research",
            "user_id": "service:workflow:other",
            "user_role": "service",
        },
        {
            "agent_id": "workflow:sales-research",
            "user_id": "service:workflow:sales-research",
            "user_role": "sales_agent",
        },
    ],
)
async def test_queue_tool_cannot_be_invoked_by_agent_or_spoofed_workflow(identity):
    ctx = SimpleNamespace(tenant_id="tenant-a", is_benchmark=False, **identity)
    result = await HANDLERS["sales_process_queue"]({"stage": "research"}, ctx)
    assert "error" in result


@pytest.mark.asyncio
async def test_queue_tool_uses_service_workflow_context_not_arguments(monkeypatch):
    from unittest.mock import AsyncMock

    import robothor.sales.queue as queue

    run = AsyncMock(return_value={"worked": False})
    tenants = []

    def driver(service):
        tenants.append(service.tenant)
        return SimpleNamespace(tick=run)

    monkeypatch.setattr(queue, "QueueDriver", driver)
    ctx = SimpleNamespace(
        tenant_id="tenant-a",
        is_benchmark=False,
        agent_id="workflow:sales-research",
        user_id="service:workflow:sales-research",
        user_role="service",
    )
    result = await HANDLERS["sales_process_queue"]({"stage": "research"}, ctx)
    assert result == {"worked": False}
    run.assert_awaited_once_with("research", "sales-research")
    assert tenants == ["tenant-a"]
    ctx.is_benchmark = True
    assert "error" in await HANDLERS["sales_process_queue"]({"stage": "research"}, ctx)
    assert run.await_count == 1

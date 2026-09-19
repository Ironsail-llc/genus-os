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
async def test_request_tool_uses_authenticated_tenant_and_never_accepts_switches(monkeypatch):
    from robothor.sales import requests

    calls = []
    monkeypatch.setattr(
        requests.Requests,
        "create",
        lambda self, data, actor: (
            calls.append((self.tenant, actor, data.target_companies)) or {"id": "request-1"}
        ),
    )
    ctx = SimpleNamespace(tenant_id="tenant-a", is_benchmark=False, agent_id="coordinator")
    args = {
        "request_key": "request-1",
        "title": "Practice research",
        "query": "US prescribing practices",
        "buying_case": "network_access",
        "target_companies": 10,
    }
    assert (await HANDLERS["sales_create_request"](args, ctx))["id"] == "request-1"
    assert calls == [("tenant-a", "agent:coordinator", 10)]
    assert "error" in await HANDLERS["sales_create_request"](args | {"sending_enabled": True}, ctx)
    ctx.is_benchmark = True
    assert "error" in await HANDLERS["sales_create_request"](args, ctx)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_request_reference_rejects_non_uuid_before_storage(monkeypatch):
    from robothor.sales import requests

    calls = []
    monkeypatch.setattr(
        requests.Requests, "get", lambda self, request_id: calls.append(request_id) or {}
    )
    ctx = SimpleNamespace(tenant_id="tenant-a", is_benchmark=False, agent_id="coordinator")
    assert "error" in await HANDLERS["sales_get_request"]({"request_id": "not-an-id"}, ctx)
    assert calls == []


@pytest.mark.asyncio
async def test_research_tool_requires_a_native_scope_and_rejects_context_overrides():
    ctx = SimpleNamespace(tenant_id="tenant-a", is_benchmark=False, agent_id="researcher")
    handler = HANDLERS["sales_research_parallel"]
    assert "error" in await handler({"buying_case": "network_access"}, ctx)
    assert "error" in await handler({"buying_case": "network_access", "topic": "send_email"}, ctx)
    ctx.is_benchmark = True
    assert "error" in await handler({"buying_case": "network_access"}, ctx)


def test_research_role_migration_allows_only_the_bounded_broker_and_research_reads(sales):
    from pathlib import Path

    from robothor.db.connection import get_connection
    from robothor.engine.permissions import check_tool_permission

    migration = Path(__file__).parents[3] / "crm/migrations/132_sales_research_delegation.sql"
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(migration.read_text())
        conn.commit()
    for name in (
        "sales_research_parallel",
        "web_search",
        "web_fetch",
        "write_file",
        "sales_get_context",
    ):
        assert check_tool_permission("sales_research_agent", sales.tenant, name) is None
    for name in (
        "spawn_agent",
        "spawn_agents",
        "sales_process_queue",
        "sales_discover",
        "exec",
        "vault_get",
        "send_email",
    ):
        assert check_tool_permission("sales_research_agent", sales.tenant, name) is not None
    assert check_tool_permission("sales_agent", sales.tenant, "sales_research_parallel") is not None


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


@pytest.mark.asyncio
async def test_coordinator_can_read_intake_configuration_without_changing_it(monkeypatch):
    from robothor.sales.requests import Requests

    monkeypatch.setattr(Requests, "workspace", lambda self: {"tenant": self.tenant})
    ctx = SimpleNamespace(tenant_id="tenant-a", is_benchmark=False, agent_id="coordinator")
    assert await HANDLERS["sales_get_workspace"]({}, ctx) == {"tenant": "tenant-a"}
    assert "error" in await HANDLERS["sales_get_workspace"]({"tenant_id": "tenant-b"}, ctx)


@pytest.mark.asyncio
async def test_report_tool_uses_authenticated_scope_and_rejects_extra_authority(monkeypatch):
    from robothor.sales.reporting import Reports

    monkeypatch.setattr(Reports, "latest", lambda self: {"tenant": self.tenant})
    ctx = SimpleNamespace(tenant_id="tenant-a", is_benchmark=False, agent_id="analyst")
    assert await HANDLERS["sales_get_report"]({}, ctx) == {"tenant": "tenant-a"}
    assert "error" in await HANDLERS["sales_get_report"]({"tenant_id": "other"}, ctx)
    assert "error" in await HANDLERS["sales_get_report"]({"report_id": "invalid"}, ctx)

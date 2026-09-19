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

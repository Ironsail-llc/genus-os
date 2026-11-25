"""MCP memory tools act as the tenant the MCP server process is bound to.

An MCP server is one stdio process per client, started with the instance's
environment. Its tenant is the one that process runs as (``ROBOTHOR_TENANT_ID``,
then ``ROBOTHOR_DEFAULT_TENANT``) -- the same binding its database connections
use for row-level security. The memory tools used to pass no tenant at all, so
an MCP client of tenant A searched and wrote the default tenant's memory.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from robothor.api.mcp import handle_tool_call

TENANT = "tenant-a"


@pytest.fixture
def bound(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", TENANT)
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", TENANT)


@pytest.fixture
def unbound_multi_tenant(monkeypatch):
    import robothor.crm.dal as dal

    monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)
    monkeypatch.delenv("ROBOTHOR_DEFAULT_TENANT", raising=False)
    monkeypatch.setattr(
        dal,
        "list_tenants",
        lambda parent_id=None, active_only=True: [{"id": "tenant-a"}, {"id": "tenant-b"}],
    )


async def test_search_memory_searches_the_bound_tenant(bound):
    with patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=[])) as sf:
        await handle_tool_call("search_memory", {"query": "tea"})
    assert sf.await_args.kwargs["tenant_id"] == TENANT


async def test_get_entity_reads_the_bound_tenant(bound):
    with patch("robothor.memory.entities.get_entity", new=AsyncMock(return_value=None)) as ge:
        await handle_tool_call("get_entity", {"name": "Alice"})
    assert ge.await_args.kwargs["tenant_id"] == TENANT


async def test_store_memory_writes_the_bound_tenant(bound):
    with patch(
        "robothor.engine.tools.handlers.memory.store_memory_content",
        new=AsyncMock(return_value={"id": 1}),
    ) as store:
        await handle_tool_call("store_memory", {"content": "Alice likes tea"})
    assert store.await_args.kwargs["tenant_id"] == TENANT


async def test_get_stats_counts_the_bound_tenant(bound):
    with patch("robothor.memory.facts.get_memory_stats", return_value={}) as stats:
        await handle_tool_call("get_stats", {})
    assert stats.call_args.kwargs["tenant_id"] == TENANT


@pytest.mark.parametrize(
    ("tool", "target", "arguments"),
    [
        ("memory_block_read", "robothor.memory.blocks.read_block", {"block_name": "persona"}),
        (
            "memory_block_write",
            "robothor.memory.blocks.write_block",
            {"block_name": "persona", "content": "x"},
        ),
        ("memory_block_list", "robothor.memory.blocks.list_blocks", {}),
        (
            "append_to_block",
            "robothor.crm.dal.append_to_block",
            {"block_name": "log", "entry": "x"},
        ),
    ],
)
async def test_block_tools_use_the_bound_tenant(bound, tool, target, arguments):
    with patch(target, return_value={}) as fn:
        await handle_tool_call(tool, arguments)
    assert fn.call_args.kwargs["tenant_id"] == TENANT


async def test_knowledge_gaps_run_bound_to_the_tenant(bound):
    from robothor.db.connection import current_tenant_scope

    seen = MagicMock()

    async def gaps():
        seen(current_tenant_scope())
        return {}

    with patch("robothor.memory.gap_analysis.analyze_knowledge_gaps", new=gaps):
        await handle_tool_call("get_knowledge_gaps", {})
    seen.assert_called_once_with(TENANT)


async def test_a_tenant_argument_cannot_redirect_a_bound_session(bound):
    with patch(
        "robothor.engine.tools.handlers.memory.store_memory_content", new=AsyncMock()
    ) as store:
        result = await handle_tool_call(
            "store_memory", {"content": "x", "content_type": "conversation", "tenant_id": "b"}
        )
    assert "tenant" in result["error"].lower()
    store.assert_not_called()


async def test_an_unbound_session_in_a_multi_tenant_deployment_is_refused(unbound_multi_tenant):
    with patch("robothor.memory.facts.search_facts", new=AsyncMock()) as sf:
        result = await handle_tool_call("search_memory", {"query": "tea"})
    assert "tenant" in result["error"].lower()
    sf.assert_not_called()


# ─── Integration ────────────────────────────────────────────────────


@pytest.mark.integration
async def test_an_mcp_block_write_lands_in_the_bound_tenant(monkeypatch):
    from robothor.db.connection import get_connection

    suffix = uuid4().hex[:10]
    tenant, block = f"test-tenant-{suffix}", f"persona-{suffix}"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (tenant, tenant))
        conn.commit()
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", tenant)
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", tenant)
    try:
        written = await handle_tool_call(
            "memory_block_write", {"block_name": block, "content": "Bob's persona"}
        )
        assert written.get("success"), written
        read = await handle_tool_call("memory_block_read", {"block_name": block})
        assert read["content"] == "Bob's persona"
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT tenant_id FROM agent_memory_blocks WHERE block_name = %s", (block,))
            assert {r[0] for r in cur.fetchall()} == {tenant}
    finally:
        with get_connection() as conn:
            cur = conn.cursor()
            # By the unique name, in every tenant: a write that went to the wrong
            # tenant must not survive the test that caught it.
            cur.execute("DELETE FROM agent_memory_blocks WHERE block_name = %s", (block,))
            cur.execute("DELETE FROM crm_tenants WHERE id = %s", (tenant,))
            conn.commit()

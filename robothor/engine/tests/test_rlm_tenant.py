"""deep_reason reads memory as the run's tenant, never the default one.

The RLM REPL helpers (search_memory, get_entity, memory_block_read) and the
context pre-loader used to call the memory functions with no tenant, so every
deep_reason session in any tenant searched the default tenant's facts.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from robothor.engine import rlm_tool

TENANT = "tenant-a"


def _fact(text="Alice prefers tea"):
    return {"fact_text": text, "category": "preference", "confidence": 0.9, "similarity": 0.8}


def _multi_tenant(monkeypatch):
    import robothor.crm.dal as dal

    monkeypatch.setattr(
        dal,
        "list_tenants",
        lambda parent_id=None, active_only=True: [{"id": "tenant-a"}, {"id": "tenant-b"}],
    )


# ─── REPL helpers ───────────────────────────────────────────────────


def test_search_memory_searches_the_given_tenant():
    with patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=[_fact()])) as sf:
        out = rlm_tool._make_search_memory_fn(tenant_id=TENANT)("tea")
    assert sf.await_args.kwargs["tenant_id"] == TENANT
    assert json.loads(out)[0]["fact"] == "Alice prefers tea"


def test_get_entity_reads_the_given_tenant():
    with patch(
        "robothor.memory.entities.get_entity", new=AsyncMock(return_value={"name": "Alice"})
    ) as ge:
        rlm_tool._make_get_entity_fn(tenant_id=TENANT)("Alice")
    assert ge.await_args.kwargs["tenant_id"] == TENANT


def test_memory_block_read_reads_the_given_tenant():
    with patch(
        "robothor.memory.blocks.read_block", return_value={"block_name": "persona", "content": "x"}
    ) as rb:
        rlm_tool._make_memory_block_read_fn(tenant_id=TENANT)("persona")
    assert rb.call_args.kwargs["tenant_id"] == TENANT


def test_the_built_tools_carry_the_tenant():
    with patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=[])) as sf:
        tools = rlm_tool._build_custom_tools("/synthetic", tenant_id=TENANT)
        tools["search_memory"]["tool"]("tea")
    assert sf.await_args.kwargs["tenant_id"] == TENANT


def test_the_helpers_run_bound_to_the_tenant_for_rls():
    """The inner model calls these from whatever thread the RLM library uses.

    Naming the tenant in the query is not enough under row-level security: the
    connection must be bound to the same tenant, or the rows are invisible. So
    each helper binds its own scope rather than relying on the caller's context.
    """
    from robothor.db.connection import current_tenant_scope

    seen = []

    async def search(*args, **kwargs):
        seen.append(current_tenant_scope())
        return []

    async def entity(*args, **kwargs):
        seen.append(current_tenant_scope())
        return

    def block(*args, **kwargs):
        seen.append(current_tenant_scope())
        return {"content": ""}

    with (
        patch("robothor.memory.facts.search_facts", new=search),
        patch("robothor.memory.entities.get_entity", new=entity),
        patch("robothor.memory.blocks.read_block", new=block),
    ):
        rlm_tool._make_search_memory_fn(tenant_id=TENANT)("tea")
        rlm_tool._make_get_entity_fn(tenant_id=TENANT)("Alice")
        rlm_tool._make_memory_block_read_fn(tenant_id=TENANT)("persona")
        for src in (
            {"type": "memory", "query": "tea"},
            {"type": "block", "block_name": "persona"},
            {"type": "entity", "name": "Alice"},
        ):
            rlm_tool._load_context_source(src, "", tenant_id=TENANT)
    assert seen == [TENANT] * 6


# ─── Context pre-loading ────────────────────────────────────────────


def test_memory_context_source_searches_the_given_tenant():
    with patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=[_fact()])) as sf:
        rlm_tool._load_context_source({"type": "memory", "query": "tea"}, "", tenant_id=TENANT)
    assert sf.await_args.kwargs["tenant_id"] == TENANT


def test_block_context_source_reads_the_given_tenant():
    with patch("robothor.memory.blocks.read_block", return_value={"content": "x"}) as rb:
        rlm_tool._load_context_source(
            {"type": "block", "block_name": "persona"}, "", tenant_id=TENANT
        )
    assert rb.call_args.kwargs["tenant_id"] == TENANT


def test_entity_context_source_reads_the_given_tenant():
    with patch(
        "robothor.memory.entities.get_entity", new=AsyncMock(return_value={"name": "Alice"})
    ) as ge:
        rlm_tool._load_context_source({"type": "entity", "name": "Alice"}, "", tenant_id=TENANT)
    assert ge.await_args.kwargs["tenant_id"] == TENANT


# ─── Entry points ───────────────────────────────────────────────────


def _rlm_modules(rlm_cls):
    logger_mod = MagicMock()
    logger_mod.RLMLogger.return_value.log_file_path = None
    rlm_mod = MagicMock()
    rlm_mod.RLM = rlm_cls
    return patch.dict("sys.modules", {"rlm": rlm_mod, "rlm.logger": logger_mod})


def test_execute_deep_reason_uses_the_configured_tenant(tmp_path):
    rlm_cls = MagicMock()
    rlm_cls.return_value.completion.return_value = SimpleNamespace(
        response="ok", usage_summary=None
    )
    with (
        _rlm_modules(rlm_cls),
        patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=[_fact()])) as sf,
    ):
        result = rlm_tool.execute_deep_reason(
            query="what does Alice drink",
            context_sources=[{"type": "memory", "query": "tea"}],
            config=rlm_tool.DeepReasonConfig(tenant_id=TENANT, log_dir=str(tmp_path)),
        )
        # The REPL tool handed to the inner model searches the same tenant.
        rlm_cls.call_args.kwargs["custom_tools"]["search_memory"]["tool"]("tea")
    assert "error" not in result
    assert [c.kwargs["tenant_id"] for c in sf.await_args_list] == [TENANT, TENANT]


def test_execute_deep_reason_without_a_tenant_is_refused_when_several_exist(monkeypatch, tmp_path):
    _multi_tenant(monkeypatch)
    rlm_cls = MagicMock()
    with _rlm_modules(rlm_cls), patch("robothor.memory.facts.search_facts") as sf:
        result = rlm_tool.execute_deep_reason(
            query="anything",
            context_sources=[{"type": "memory", "query": "tea"}],
            config=rlm_tool.DeepReasonConfig(log_dir=str(tmp_path)),
        )
    assert "tenant" in result["error"].lower()
    sf.assert_not_called()
    rlm_cls.assert_not_called()


async def test_the_tool_handler_passes_the_run_context_tenant():
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.reasoning import _deep_reason

    with patch(
        "robothor.engine.rlm_tool.execute_deep_reason", return_value={"response": "ok"}
    ) as d:
        await _deep_reason({"query": "q"}, ToolContext(agent_id="main", tenant_id=TENANT))
    assert d.call_args.kwargs["config"].tenant_id == TENANT


def test_deep_mode_passes_the_run_tenant(monkeypatch):
    import robothor.engine.runtime.controls as controls
    from robothor.engine.runtime.deep_admission import execute_deep_checked

    monkeypatch.setattr(controls, "stopped", lambda *a, **k: False)
    run = SimpleNamespace(id=str(uuid4()), tenant_id=TENANT)
    with patch(
        "robothor.engine.rlm_tool.execute_deep_reason", return_value={"response": "ok"}
    ) as d:
        execute_deep_checked(run=run, workspace="/synthetic", query="q")
    assert d.call_args.kwargs["config"].tenant_id == TENANT


# ─── Integration: real rows, two tenants ────────────────────────────


@pytest.fixture
def two_tenants():
    from robothor.db.connection import get_connection

    suffix = uuid4().hex[:10]
    a, b = f"test-tenant-a-{suffix}", f"test-tenant-b-{suffix}"
    with get_connection() as conn:
        cur = conn.cursor()
        for t in (a, b):
            cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (t, t))
        conn.commit()
    try:
        yield a, b
    finally:
        with get_connection() as conn:
            cur = conn.cursor()
            for table in ("agent_memory_blocks", "memory_entities"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id IN (%s, %s)", (a, b))  # noqa: S608
            cur.execute("DELETE FROM crm_tenants WHERE id IN (%s, %s)", (a, b))
            conn.commit()


@pytest.mark.integration
def test_deep_reason_block_read_returns_only_its_tenants_row(two_tenants):
    from robothor.memory.blocks import write_block

    a, b = two_tenants
    assert write_block("persona", "tenant A persona", tenant_id=a).get("success")
    assert write_block("persona", "tenant B persona", tenant_id=b).get("success")

    out = json.loads(rlm_tool._make_memory_block_read_fn(tenant_id=a)("persona"))
    preload = rlm_tool._load_context_source(
        {"type": "block", "block_name": "persona"}, "", tenant_id=a
    )
    assert out["content"] == "tenant A persona"
    assert "tenant A persona" in preload
    assert "tenant B persona" not in preload


@pytest.mark.integration
def test_deep_reason_entity_lookup_returns_only_its_tenants_row(two_tenants):
    from robothor.db.connection import get_connection

    a, b = two_tenants
    name = f"Alice {uuid4().hex[:6]}"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO memory_entities (name, entity_type, tenant_id) VALUES (%s, 'person', %s)",
            (name, a),
        )
        conn.commit()

    found = json.loads(rlm_tool._make_get_entity_fn(tenant_id=a)(name))
    other = json.loads(rlm_tool._make_get_entity_fn(tenant_id=b)(name))
    assert found["tenant_id"] == a
    assert other["found"] is False


def test_deep_reason_cannot_be_pointed_at_another_tenant():
    """The tenant its helpers bind is the run's own: a `tenant` argument is refused.

    ``rlm_tool._as_tenant`` is pinned in tests/test_cross_tenant_scope_callers.py
    on exactly this ground, so it is asserted here rather than assumed.
    """
    from robothor.engine.tools import cross_tenant
    from robothor.engine.tools.dispatch import ToolContext

    ctx = ToolContext(agent_id="main", tenant_id=TENANT)
    refused = cross_tenant.admit("deep_reason", {"query": "q", "tenant": "tenant-b"}, ctx)
    assert isinstance(refused, dict)
    assert "error" in refused


def test_the_helpers_take_no_tenant_from_their_caller():
    """The inner model calls the helpers; none of them accepts a tenant."""
    import inspect

    for factory in (
        rlm_tool._make_search_memory_fn,
        rlm_tool._make_get_entity_fn,
        rlm_tool._make_memory_block_read_fn,
    ):
        params = inspect.signature(factory(tenant_id=TENANT)).parameters
        assert "tenant_id" not in params and "tenant" not in params
        assert not any(p.kind is p.VAR_KEYWORD for p in params.values())

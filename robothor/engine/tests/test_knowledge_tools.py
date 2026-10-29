"""kb_* tool handlers: argument mapping, tenant from the context, errors.

The store is monkeypatched: what the store does with the rows is tested in
robothor/knowledge/tests/.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.dispatch import ToolContext, builtin_handlers
from robothor.knowledge import search as kb_search
from robothor.knowledge import store

KB_TOOLS = ("kb_search", "kb_upsert_document", "kb_delete_document", "kb_list_collections")


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, tuple, dict]] = []

    async def fake_upsert(*args, **kwargs):
        seen.append(("upsert", args, kwargs))
        return {"status": "created", "version": 1, "chunks": 2}

    async def fake_delete(*args, **kwargs):
        seen.append(("delete", args, kwargs))
        return True

    async def fake_list(*args, **kwargs):
        seen.append(("list", args, kwargs))
        return [
            {
                "collection": "helpdesk/policies",
                "documents": 1,
                "chunks": 2,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    async def fake_search(*args, **kwargs):
        seen.append(("search", args, kwargs))
        return [
            {
                "doc_id": "refunds",
                "title": "Refunds",
                "chunk_ix": 0,
                "text": "t",
                "score": 0.03,
                "source_uri": "",
            }
        ]

    monkeypatch.setattr(store, "upsert_document", fake_upsert)
    monkeypatch.setattr(store, "delete_document", fake_delete)
    monkeypatch.setattr(store, "list_collections", fake_list)
    monkeypatch.setattr(kb_search, "search", fake_search)
    return seen


def _ctx(**kw) -> ToolContext:
    return ToolContext(agent_id="main", tenant_id="tenant-a", **kw)


async def _call(name, args, ctx=None):
    return await builtin_handlers()[name](args, ctx or _ctx())


def test_every_kb_tool_is_a_builtin_with_a_schema():
    from robothor.engine.tools.schemas import get_engine_schemas

    handlers = builtin_handlers()
    schemas = get_engine_schemas()
    for name in KB_TOOLS:
        assert name in handlers
        assert schemas[name]["function"]["name"] == name


def test_reads_are_readonly_and_writes_are_not():
    from robothor.engine.tools.constants import READONLY_TOOLS

    assert {"kb_search", "kb_list_collections"} <= READONLY_TOOLS
    assert not {"kb_upsert_document", "kb_delete_document"} & READONLY_TOOLS


@pytest.mark.asyncio
async def test_kb_search_passes_tenant_collection_query_and_limit(calls):
    out = await _call(
        "kb_search", {"collection": "helpdesk/policies", "query": "refunds", "limit": 3}
    )
    assert out["results"][0]["doc_id"] == "refunds"
    assert out["count"] == 1
    _kind, args, kwargs = calls[0]
    assert args[:3] == ("tenant-a", "helpdesk/policies", "refunds")
    assert kwargs.get("limit", args[3] if len(args) > 3 else None) == 3


@pytest.mark.asyncio
async def test_kb_search_requires_collection_and_query(calls):
    assert "error" in await _call("kb_search", {"query": "refunds"})
    assert "error" in await _call("kb_search", {"collection": "helpdesk/policies"})
    assert calls == []


@pytest.mark.asyncio
async def test_kb_upsert_document_maps_arguments(calls):
    out = await _call(
        "kb_upsert_document",
        {
            "collection": "helpdesk/policies",
            "doc_id": "refunds",
            "title": "Refunds",
            "text": "Refunds within 14 days.",
            "source_uri": "https://example.com/r",
        },
    )
    assert out == {
        "collection": "helpdesk/policies",
        "doc_id": "refunds",
        "status": "created",
        "version": 1,
        "chunks": 2,
    }
    _kind, args, kwargs = calls[0]
    assert args[:5] == (
        "tenant-a",
        "helpdesk/policies",
        "refunds",
        "Refunds",
        "Refunds within 14 days.",
    )
    assert kwargs.get("source_uri") == "https://example.com/r"


@pytest.mark.asyncio
async def test_kb_delete_document_reports_whether_it_existed(calls):
    out = await _call(
        "kb_delete_document", {"collection": "helpdesk/policies", "doc_id": "refunds"}
    )
    assert out == {"collection": "helpdesk/policies", "doc_id": "refunds", "deleted": True}
    assert calls[0][1][:3] == ("tenant-a", "helpdesk/policies", "refunds")


@pytest.mark.asyncio
async def test_kb_list_collections_is_per_tenant(calls):
    out = await _call("kb_list_collections", {})
    assert out["collections"][0]["collection"] == "helpdesk/policies"
    assert calls[0][1][0] == "tenant-a"


@pytest.mark.asyncio
async def test_a_validation_error_comes_back_as_a_tool_error(monkeypatch):
    async def refuse(*_a, **_kw):
        raise ValueError("collection must look like <plugin>/<topic>")

    monkeypatch.setattr(store, "upsert_document", refuse)
    out = await _call(
        "kb_upsert_document", {"collection": "Bad Name", "doc_id": "x", "title": "X", "text": "y"}
    )
    assert "<plugin>/<topic>" in out["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["kb_upsert_document", "kb_delete_document"])
async def test_writes_are_refused_in_a_benchmark_run(calls, name):
    out = await _call(
        name,
        {"collection": "helpdesk/policies", "doc_id": "refunds", "title": "Refunds", "text": "t"},
        _ctx(is_benchmark=True),
    )
    assert "error" in out
    assert calls == []

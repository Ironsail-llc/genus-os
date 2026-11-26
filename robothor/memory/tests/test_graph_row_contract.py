"""A row the graph leg alone found has the same keys as any other fact row.

Graph-only rows used to lack ``confidence`` (and other fact columns), so every
consumer that formats ``search_facts`` rows by key raised ``KeyError`` as soon
as one took a reserved place: the MCP ``search_memory`` tool, the deep-reason
tool's ``search_memory`` and its memory context loader. Real Postgres; the row
is produced by the real ``search_facts`` and then handed to each consumer.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory.tests.test_graph_activation import TENANT, Graph
from robothor.memory.tests.test_graph_recall_leg import _chain, _scorer, world  # noqa: F401

pytestmark = pytest.mark.integration

QUERY = "Alice Smith quarterly logistics agreement"


def _fact_columns() -> set[str]:
    """The keys every fact leg's rows carry (``facts._FACT_COLUMNS``)."""
    from robothor.memory import facts

    cols = facts._FACT_COLUMNS
    names = {c.strip() for c in re.sub(r"EXTRACT\(.*?\)\) as \w+", "", cols).split(",")}
    return {n for n in names if n} | {"age_seconds"}


def _graph_only_rows(world: Graph, monkeypatch) -> tuple[list[dict[str, Any]], int]:
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    w = _chain(world)
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("northern depot"))
    rows = asyncio.run(search_facts(QUERY, limit=5, tenant_id=TENANT, use_reranker=True))
    far = next((r for r in rows if r["id"] == w["far"]), None)
    assert far is not None and set(far["leg_ranks"]) == {"graph"}, "negative control"
    return rows, w["far"]


def test_a_graph_only_row_carries_every_fact_column(world: Graph, monkeypatch) -> None:
    rows, far = _graph_only_rows(world, monkeypatch)
    row = next(r for r in rows if r["id"] == far)
    missing = _fact_columns() - set(row)
    assert not missing, f"graph-only row lacks {sorted(missing)}"
    assert row["confidence"] is not None


def test_mcp_search_memory_formats_a_graph_only_row(world: Graph, monkeypatch) -> None:
    from robothor.api.mcp import handle_tool_call

    rows, _far = _graph_only_rows(world, monkeypatch)
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", TENANT)
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", TENANT)
    with patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=rows)):
        out = asyncio.run(handle_tool_call("search_memory", {"query": QUERY}))
    assert len(out["results"]) == len(rows)
    assert all("confidence" in r for r in out["results"])


def test_deep_reason_search_and_context_loader_format_a_graph_only_row(
    world: Graph, monkeypatch
) -> None:
    from robothor.engine.rlm_tool import _load_context_source, _make_search_memory_fn

    rows, _far = _graph_only_rows(world, monkeypatch)
    with patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=rows)):
        listed = _make_search_memory_fn(tenant_id=TENANT)(QUERY)
        loaded = _load_context_source({"type": "memory", "query": QUERY}, "", tenant_id=TENANT)
    assert "northern depot" in listed
    assert loaded is not None and "northern depot" in loaded

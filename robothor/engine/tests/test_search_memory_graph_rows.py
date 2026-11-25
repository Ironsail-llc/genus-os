"""search_memory and the graph leg: the reader, the labels, read-only accounting.

The tool passes the caller's person id to recall as the reader whose private
evidence the graph walk may cross, on both read paths. A row the graph leg
found comes back labelled with its origin and the path that reached it. A row
found only by the graph is not logged as fact access: recall stays read-only
and graph association is not a retrieval the agent asked for by content.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import memory as memmod
from robothor.identity.context import IdentityContext
from robothor.memory.router import _normalize_fact

IDENTITY = IdentityContext(
    tenant_id="test-tenant",
    channel="webchat",
    identifier="acct-1",
    verified=True,
    display_name="Alice Smith",
    person_id="11111111-1111-1111-1111-111111111111",
    role="owner",
)

GRAPH_ROW: dict[str, Any] = {
    "id": 7,
    "kind": "fact",
    "fact_text": "Carol White runs the northern depot",
    "category": "event",
    "source": "graph",
    "origin": "external",
    "graph_hops": 2,
    "graph_path": ["Alice Smith", "Bob Jones", "Carol White"],
    "rrf_score": 0.01,
}
PLAIN_ROW: dict[str, Any] = {
    "id": 3,
    "kind": "fact",
    "fact_text": "Alice Smith keeps the passport",
    "category": "event",
    "rrf_score": 0.03,
}


@pytest.fixture
def fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)


@pytest.fixture
def routed_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", "1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("fallback_path")
async def test_the_fallback_path_passes_the_reader() -> None:
    sf = AsyncMock(return_value=[])
    with patch("robothor.memory.facts.search_facts", sf):
        await memmod.HANDLERS["search_memory"](
            {"query": "x"}, ToolContext(tenant_id="test-tenant", identity=IDENTITY)
        )
    assert sf.call_args.kwargs["reader_scope_id"] == IDENTITY.person_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("routed_path")
async def test_the_routed_path_passes_the_reader() -> None:
    sf = AsyncMock(return_value=[])
    with patch("robothor.memory.facts.search_facts", sf):
        await memmod.HANDLERS["search_memory"](
            {"query": "x"}, ToolContext(tenant_id="test-tenant", identity=IDENTITY)
        )
    assert sf.call_args.kwargs["reader_scope_id"] == IDENTITY.person_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("fallback_path")
async def test_no_identity_means_no_reader() -> None:
    sf = AsyncMock(return_value=[])
    with patch("robothor.memory.facts.search_facts", sf):
        await memmod.HANDLERS["search_memory"]({"query": "x"}, ToolContext(tenant_id="t"))
    assert sf.call_args.kwargs["reader_scope_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["fallback_path", "routed_path"])
async def test_a_graph_row_is_labelled_with_its_origin_and_path(
    path: str, request: pytest.FixtureRequest
) -> None:
    request.getfixturevalue(path)
    sf = AsyncMock(return_value=[dict(PLAIN_ROW), dict(GRAPH_ROW)])
    with patch("robothor.memory.facts.search_facts", sf):
        out = await memmod.HANDLERS["search_memory"](
            {"query": "x"}, ToolContext(tenant_id="test-tenant")
        )
    rows = {r["id"]: r for r in out["results"]}
    assert rows[7]["source"] == "graph"
    assert rows[7]["origin"] == "external"
    assert rows[7]["via"] == ["Alice Smith", "Bob Jones", "Carol White"]
    assert "via" not in rows[3] and "origin" not in rows[3]


def test_the_router_keeps_the_graph_labels() -> None:
    row = _normalize_fact(dict(GRAPH_ROW))
    assert row["source"] == "graph"
    assert row["origin"] == "external"
    assert row["graph_path"] == GRAPH_ROW["graph_path"]
    assert "graph_path" not in _normalize_fact(dict(PLAIN_ROW))


@pytest.mark.asyncio
@pytest.mark.usefixtures("fallback_path")
async def test_a_graph_only_row_is_not_logged_as_fact_access() -> None:
    sf = AsyncMock(return_value=[dict(PLAIN_ROW), dict(GRAPH_ROW)])
    log = MagicMock()
    with (
        patch("robothor.memory.facts.search_facts", sf),
        patch("robothor.memory.outcomes.log_fact_access", log),
    ):
        await memmod.HANDLERS["search_memory"](
            {"query": "x"}, ToolContext(tenant_id="test-tenant", run_id="run-1")
        )
    logged = log.call_args.args[1]
    assert logged == [3]

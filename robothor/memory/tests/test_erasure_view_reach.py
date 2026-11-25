"""The admin erasure view of ``search_facts`` is the tenant admin's alone.

``admin_erasure_view=True`` drops the owner predicate, so a private fact can be
found and forgotten from the bridge's admin fact list. Nothing an agent, an MCP
client or a tool argument controls may turn it on:

* only ``search_facts`` itself and the admin fact search route name it;
* it is keyword-only and off by default;
* ``search_memory`` (tool handler and MCP tool) never forwards it, whatever
  the arguments say;
* it refuses a restricted reader scope.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SWITCH = "admin_erasure_view"
ALLOWED = {
    Path("robothor/memory/facts.py"),
    Path("crm/bridge/routers/memory_facts.py"),
}


def _sources() -> list[Path]:
    out: list[Path] = []
    for base in ("robothor", "crm"):
        for path in (ROOT / base).rglob("*.py"):
            rel = path.relative_to(ROOT)
            if "tests" in rel.parts or rel.name.startswith("test_") or rel.name == "conftest.py":
                continue
            out.append(rel)
    return out


def test_only_the_search_and_the_admin_route_name_the_switch() -> None:
    named = {rel for rel in _sources() if SWITCH in (ROOT / rel).read_text(encoding="utf-8")}
    assert named == ALLOWED


def test_the_switch_is_keyword_only_and_off_by_default() -> None:
    from robothor.memory.facts import search_facts

    param = inspect.signature(search_facts).parameters[SWITCH]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False


def test_no_tool_schema_offers_the_switch() -> None:
    from robothor.engine.tools import schemas

    assert SWITCH not in inspect.getsource(schemas)


@pytest.fixture
def spy(monkeypatch) -> list[dict[str, Any]]:
    from robothor.memory import facts

    calls: list[dict[str, Any]] = []

    async def _search(query: str, *_a: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(facts, "search_facts", _search)
    monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)
    return calls


async def test_the_search_memory_tool_never_forwards_the_switch(spy) -> None:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.memory import HANDLERS

    ctx = ToolContext(agent_id="main", run_id="", tenant_id="test-tenant", user_id="")
    await HANDLERS["search_memory"]({"query": "door code", SWITCH: True}, ctx, record_access=False)
    assert spy, "the handler searched"
    assert all(not call.get(SWITCH) for call in spy)


async def test_the_mcp_search_memory_tool_never_forwards_the_switch(spy, monkeypatch) -> None:
    from robothor.api import mcp

    async def _tenant(_arguments: dict[str, Any]) -> str:
        return "test-tenant"

    monkeypatch.setattr(mcp, "_session_tenant", _tenant)
    await mcp.handle_tool_call("search_memory", {"query": "door code", SWITCH: True})
    assert spy, "the MCP tool searched"
    assert all(not call.get(SWITCH) for call in spy)


async def test_the_switch_refuses_a_restricted_reader() -> None:
    from robothor.identity.scope import DataScope
    from robothor.memory.facts import search_facts

    scope = DataScope(
        tenant_id="test-tenant",
        person_id="00000000-0000-0000-0000-000000000001",
        restricted=True,
    )
    with pytest.raises(ValueError, match="erasure"):
        await search_facts("door code", tenant_id="test-tenant", scope=scope, **{SWITCH: True})

"""The recall context cap is a documented setting, read once per process.

``ROBOTHOR_MEMORY_RECALL_CONTEXT_CHARS`` (default 4,000, the value the
constant had) sets how many characters of memory one recall returns, on every
surface that packs to it: the ``search_memory`` tool on both paths, the routed
recall and MCP ``search_memory``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from robothor.memory import facts, recall
from robothor.memory.tests.test_recall_budget_fill import _call, _rows, tool
from robothor.settings import get_settings, reset_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["tool"]

ENV = "ROBOTHOR_MEMORY_RECALL_CONTEXT_CHARS"


@pytest.fixture
def cap(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """A clean settings cache before and after: the cap is read once."""
    monkeypatch.delenv(ENV, raising=False)
    reset_settings()
    yield monkeypatch
    reset_settings()


def _used(rows: list[dict[str, Any]], key: str = "fact_text") -> int:
    return sum(len(r[key]) for r in rows)


def test_the_default_is_the_old_constant(cap: pytest.MonkeyPatch) -> None:
    assert get_settings().engine.recall_context_chars == 4000
    assert recall.RECALL_CONTEXT_CHARS == 4000
    assert recall.recall_context_chars() == 4000


def test_the_setting_sets_the_cap_the_context_is_packed_to(cap: pytest.MonkeyPatch) -> None:
    cap.setenv(ENV, "1000")
    reset_settings()
    kept = recall.within_context_cap(_rows(30, 100))
    assert _used(kept) <= 1000
    assert _used(kept) > 900, "filled to the configured cap"


def test_the_cap_is_read_once_per_process(cap: pytest.MonkeyPatch) -> None:
    cap.setenv(ENV, "1000")
    reset_settings()
    first = recall.within_context_cap(_rows(30, 100))
    cap.setenv(ENV, "2500")
    again = recall.within_context_cap(_rows(30, 100))
    assert _used(again) == _used(first), "a later change waits for a restart"


@pytest.mark.asyncio
async def test_the_routed_recall_packs_to_the_setting(
    cap: pytest.MonkeyPatch,
) -> None:
    from robothor.memory import router

    async def fake_search(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return _rows(50, 100)

    cap.setattr(facts, "search_facts", fake_search)
    cap.setenv(ENV, "1500")
    reset_settings()
    out = await router.recall("garage locker code", tenant_id="test-tenant", limit=50)
    used = sum(len(r["text"]) for r in out["results"])
    assert 1400 < used <= 1500


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
async def test_the_tool_packs_to_the_setting(
    tool: list[dict[str, Any]], cap: pytest.MonkeyPatch, routed: bool
) -> None:
    if routed:
        cap.setenv("ROBOTHOR_RIP_15_ENABLED", "1")
    else:
        cap.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)
    cap.setenv(ENV, "1500")
    reset_settings()
    out = await _call({"query": "garage locker code"})
    used = sum(len(r["fact"]) for r in out["results"])
    assert 1400 < used <= 1500


@pytest.mark.asyncio
async def test_mcp_search_memory_packs_to_the_setting(
    tool: list[dict[str, Any]], cap: pytest.MonkeyPatch
) -> None:
    from robothor.api import mcp

    async def _tenant(_arguments: dict[str, Any]) -> str:
        return "test-tenant"

    cap.setattr(mcp, "_session_tenant", _tenant)
    cap.setenv(ENV, "1500")
    reset_settings()
    out = await mcp.handle_tool_call("search_memory", {"query": "garage locker code"})
    used = sum(len(r["fact"]) for r in out["results"])
    assert 1400 < used <= 1500

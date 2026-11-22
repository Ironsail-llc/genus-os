"""A workspace run is handed the tool it exists to call.

A report's run publishes its edition with ``room_publish_edition``. On an agent
whose toolset is deferred (the main agent) that tool was reachable only through
``tool_search``, and on an agent with an explicit ``tools_allowed`` it was not
reachable at all. A scheduled run whose service role may not search then ended
"without publishing an edition". Inside a workspace run the tool is part of
the toolset, advertised with its schema; outside one nothing changes.
"""

from __future__ import annotations

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.tools.registry import ToolRegistry
from robothor.rooms.run_context import RoomRun, room_run_scope

PUBLISH = "room_publish_edition"


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry()


def _run() -> RoomRun:
    return RoomRun(tenant_id="test-tenant", room_id="r1", session_key="agent:main:room:r1")


def _advertised(registry: ToolRegistry, config: AgentConfig) -> set[str]:
    return {s["function"]["name"] for s in registry.build_for_agent(config)}


def test_a_deferred_agent_is_advertised_the_publish_tool_in_a_workspace_run(registry) -> None:
    main = AgentConfig(id="main", name="Main")
    assert registry.should_defer(main)
    assert PUBLISH not in _advertised(registry, main)

    with room_run_scope(_run()):
        assert PUBLISH in _advertised(registry, main)
        assert PUBLISH in registry.deferred_whitelist(main)


def test_an_agent_with_its_own_tool_list_gets_it_in_a_workspace_run(registry) -> None:
    analyst = AgentConfig(id="analyst", name="Analyst", tools_allowed=["web_fetch"])
    assert PUBLISH not in registry.get_tool_names(analyst)

    with room_run_scope(_run()):
        assert PUBLISH in registry.get_tool_names(analyst)
        assert PUBLISH in _advertised(registry, analyst)

    assert PUBLISH not in registry.get_tool_names(analyst)


def test_a_manifest_that_denies_it_still_wins(registry) -> None:
    quiet = AgentConfig(id="quiet", name="Quiet", tools_allowed=["web_fetch"], tools_denied=[PUBLISH])
    with room_run_scope(_run()):
        assert PUBLISH not in registry.get_tool_names(quiet)


def test_nothing_else_is_added(registry) -> None:
    analyst = AgentConfig(id="analyst", name="Analyst", tools_allowed=["web_fetch"])
    outside = set(registry.get_tool_names(analyst))
    with room_run_scope(_run()):
        inside = set(registry.get_tool_names(analyst))
    assert inside - outside == {PUBLISH}

"""``tool_search`` hands over a schema only when it is sure of the tool.

The top hit's parameters come inline with a hint to ``tool_call`` it directly,
and one such call puts the tool in the agent's recent tools, so it is
advertised on every later turn. That makes the top hit a recommendation, and
it was not earning one: on a broad deferred agent "five most urgent open
tasks" ranked a coding tool that opens a pull request first — a keyword hit on
"open" — and ``list_tasks`` eleventh. Three rules follow:

1. the thing the query asks about, when it is a token of a tool's NAME,
   outranks a keyword or description hit on a rarer modifier (``tasks``
   reaches the task family, not a tool whose keywords say "open");
2. an opt-in tool — one an agent is given only by naming it, such as the
   ``claude_code_*`` family — never outranks a plain tool of equal score;
3. the schema comes inline only when the top hit clearly leads the second;
   otherwise the hits come back bare and the hint keeps ``tool_describe``.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.constants import OPT_IN_TOOLS, TOOLSEARCH_TOOLS
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import toolsearch
from robothor.engine.tools.registry import ToolRegistry, get_registry

URGENT = "five most urgent open tasks"

#: The coding driver's publish step, as its own branch declares it: its
#: vocabulary and description say "open" (a pull request); its name does not.
PUBLISH_LIKE = "claude_code_publish"


def _names(schemas: list[dict]) -> set[str]:
    return {s["function"]["name"] for s in schemas}


def _cfg(allowed: list[str]):
    from robothor.engine.config import manifest_to_agent_config

    return manifest_to_agent_config({"id": "broad", "name": "broad", "tools_allowed": allowed})


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS"):
        monkeypatch.delenv(name, raising=False)


def _schema(name: str, description: str, keywords: tuple[str, ...] = ()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "keywords": list(keywords),
            "parameters": {"type": "object", "properties": {}},
        },
    }


async def _search(query: str, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Search as a broad deferred agent allowed every registered tool."""
    from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

    registry = get_registry()
    monkeypatch.setitem(
        registry._schemas,
        PUBLISH_LIKE,
        _schema(
            PUBLISH_LIKE,
            "Use this after a claude_code job is done with its verify passed: pushes the "
            "job's branch to the repository's allowlisted origin and opens a DRAFT pull "
            "request, returning its URL. It never merges. Owner or admin only.",
            (
                *("code", "coding", "claude", "bug", "fix", "test", "repo", "commit", "job"),
                *("github", "pr", "pull", "code", "publish", "push", "open", "draft"),
            ),
        ),
    )
    allowed = [n for n in sorted(registry._schemas) if n not in TOOLSEARCH_TOOLS]
    cfg = _cfg(allowed)
    assert registry.should_defer(cfg), "precondition: a broad agent is deferred"
    schemas = registry.build_for_agent(cfg, recent=frozenset())
    tokens = publish_toolset(registry, cfg, registry.get_tool_names(cfg), _names(schemas))
    try:
        return await toolsearch._tool_search({"query": query}, ToolContext(agent_id=cfg.id))
    finally:
        withdraw_toolset(tokens)


class TestANameMatchLeads:
    @pytest.mark.asyncio
    async def test_urgent_open_tasks_finds_list_tasks_with_its_schema(self, monkeypatch) -> None:
        found = await _search(URGENT, monkeypatch)
        names = [h["name"] for h in found["results"]]

        assert names[0] == "list_tasks", names
        assert found["results"][0]["in_toolset"] is False, "precondition: it is deferred"
        expected = get_registry().get_schema("list_tasks")["function"]["parameters"]
        assert found["results"][0]["parameters"] == expected
        assert "list_tasks's parameters are included" in found["hint"]

    @pytest.mark.asyncio
    async def test_a_keyword_only_opt_in_hit_falls_below_the_task_family(self, monkeypatch) -> None:
        found = await _search(URGENT, monkeypatch)
        names = [h["name"] for h in found["results"]]

        assert PUBLISH_LIKE not in names[:5], names
        assert {"list_tasks", "get_task"} <= set(names), names


class TestAVagueQueryGetsNoSchema:
    @pytest.mark.asyncio
    async def test_do_the_thing_returns_bare_hits_and_the_describe_hint(self, monkeypatch) -> None:
        found = await _search("do the thing", monkeypatch)

        assert found["results"], "precondition: the query matched something"
        assert all("parameters" not in hit for hit in found["results"])
        assert "parameters are included" not in found["hint"]
        assert "tool_describe" in found["hint"]


class TestAnOptInToolNeverWinsATie:
    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        description = "Frobnicate the quarterly widget ledger."
        # The gated name sorts FIRST, so an alphabetical tie-break favours it.
        gated = "claude_code_cancel"
        assert gated in OPT_IN_TOOLS, "precondition: the coding driver is opt-in"
        registry._schemas = {
            gated: _schema(gated, description),
            "zz_widget_report": _schema("zz_widget_report", description),
        }
        return registry

    def test_the_plain_tool_ranks_first_on_an_equal_score(self) -> None:
        registry = self._registry()
        hits = registry.search_tools(list(registry._schemas), "frobnicate ledger")
        assert [h["name"] for h in hits] == ["zz_widget_report", "claude_code_cancel"]

"""A deferred agent's everyday tools cost no search round-trip.

Measured on a live instance with a production model: "the five most urgent
open CRM tasks" took about 45 s at the median, 4-9 LLM calls and 174k-511k
input tokens, because ``list_tasks`` was deferred while its neighbours were
in the core, so every run went tool_search, tool_search, tool_describe,
tool_call. Two changes: a manifest's ``tools_eager`` names tools that are
always in its advertised core, recent history or not; and ``tool_search``
returns its top hit's parameters inline, so the common case needs no
``tool_describe``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from robothor.engine.tools.constants import CORE_TOOLS, TOOLSEARCH_TOOLS
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import toolsearch
from robothor.engine.tools.registry import ToolRegistry, get_registry

REPO = Path(__file__).resolve().parents[3]
MAIN_EAGER = {"list_tasks", "get_task", "update_task", "gws_gmail_search", "gws_gmail_get"}


def _names(schemas: list[dict]) -> set[str]:
    return {s["function"]["name"] for s in schemas}


def _cfg(*, allowed: list[str] | None = None, eager: list[str] | None = None, agent_id="broad"):
    from robothor.engine.config import manifest_to_agent_config

    manifest: dict = {"id": agent_id, "name": agent_id}
    if allowed is not None:
        manifest["tools_allowed"] = allowed
    if eager is not None:
        manifest["tools_eager"] = eager
    return manifest_to_agent_config(manifest)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS"):
        monkeypatch.delenv(name, raising=False)


def _broad(registry: ToolRegistry) -> list[str]:
    return [n for n in sorted(registry._schemas) if n not in TOOLSEARCH_TOOLS][:150]


class TestEagerTools:
    def test_the_manifest_key_reaches_the_config(self) -> None:
        assert _cfg(eager=["list_tasks", " ", "get_task"]).tools_eager == ["list_tasks", "get_task"]

    def test_an_eager_tool_is_advertised_with_no_recent_history(self) -> None:
        registry = ToolRegistry()
        allowed = _broad(registry) + ["list_tasks"]
        assert "list_tasks" not in CORE_TOOLS, "precondition: not core already"
        cfg = _cfg(allowed=allowed, eager=["list_tasks"])

        assert registry.should_defer(cfg)
        advertised = _names(registry.build_for_agent(cfg, recent=frozenset()))
        assert "list_tasks" in advertised
        assert advertised >= TOOLSEARCH_TOOLS

    def test_an_eager_tool_outside_the_allowed_set_is_not(self) -> None:
        registry = ToolRegistry()
        allowed = [n for n in _broad(registry) if n != "list_tasks"]
        cfg = _cfg(allowed=allowed, eager=["list_tasks"])
        assert "list_tasks" not in _names(registry.build_for_agent(cfg))

    def test_mains_template_makes_its_everyday_tools_eager(self) -> None:
        text = (REPO / "templates/agents/core/main/manifest.template.yaml").read_text()
        manifest = yaml.safe_load(text.replace("{{ timezone }}", "UTC"))
        assert MAIN_EAGER <= set(manifest["tools_eager"])
        registry = ToolRegistry()
        assert MAIN_EAGER <= set(registry._schemas), "every eager name is a real tool"

    def test_the_deferred_set_stays_well_below_the_full_set(self, monkeypatch) -> None:
        from robothor.engine.context import estimate_tokens

        def tokens(schemas: list[dict]) -> int:
            return estimate_tokens([{"role": "system", "content": json.dumps(schemas)}])

        registry = ToolRegistry()
        main = _cfg(agent_id="main", eager=sorted(MAIN_EAGER))
        monkeypatch.setenv("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "100000")
        full = registry.build_for_agent(main)
        monkeypatch.delenv("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD")
        deferred = registry.build_for_agent(main)

        assert MAIN_EAGER <= _names(deferred)
        assert tokens(deferred) < tokens(full) * 0.35


class TestToolSearchCarriesTheTopHitsSchema:
    @pytest.mark.asyncio
    async def test_the_top_deferred_hit_comes_with_its_parameters(self) -> None:
        from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

        registry = get_registry()
        cfg = _cfg(allowed=_broad(registry))
        schemas = registry.build_for_agent(cfg)
        tokens = publish_toolset(registry, cfg, registry.get_tool_names(cfg), _names(schemas))
        try:
            found = await toolsearch._tool_search(
                {"query": "search my email"}, ToolContext(agent_id=cfg.id)
            )
            top = found["results"][0]
            described = await toolsearch._tool_describe(
                {"name": top["name"]}, ToolContext(agent_id=cfg.id)
            )
        finally:
            withdraw_toolset(tokens)

        assert top["in_toolset"] is False, "precondition: the top hit is deferred"
        assert top["parameters"] == described["parameters"]
        assert "tool_describe" not in found["hint"].split(".")[0]
        assert all("parameters" not in hit for hit in found["results"][1:])

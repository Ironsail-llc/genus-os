"""What a broad agent is sent once its toolset is deferred, and how a plugin
keeps its own general-purpose tools loaded.

An agent with no ``tools_allowed`` may use every registered tool. Above
``ROBOTHOR_DEFERRED_TOOLS_THRESHOLD`` (40) its schemas are deferred: the model
is shown the core plus ``tool_search`` / ``tool_describe`` / ``tool_call``, and
a plugin tool costs a search round-trip on first use. A plugin pins the handful
it wants always loaded through the agent's ``tools_eager`` list, which a
contract-2.0 patch can ``append`` to.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.constants import CORE_TOOLS, TOOLSEARCH_TOOLS
from robothor.engine.tools.registry import ToolRegistry

PLUGIN_TOOLS = ("example_catalog_search", "example_order_lookup", "example_customer_get")


def _names(schemas: list[dict]) -> set[str]:
    return {s["function"]["name"] for s in schemas}


def _cfg(manifest: dict):
    from robothor.engine.config import manifest_to_agent_config

    return manifest_to_agent_config({"name": manifest["id"], **manifest})


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    for name in ("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS"):
        monkeypatch.delenv(name, raising=False)
    reg = ToolRegistry()
    for name in PLUGIN_TOOLS:
        reg._schemas[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Example plugin tool {name}.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        reg._plugin_schema_names.add(name)
    return reg


class TestABroadAgentIsSentTheCoreOnly:
    def test_no_allowlist_and_many_tools_means_deferred(self, registry):
        cfg = _cfg({"id": "broad"})
        assert len(registry.get_tool_names(cfg)) > 40
        assert registry.should_defer(cfg)

        advertised = _names(registry.build_for_agent(cfg, recent=frozenset()))

        assert advertised >= TOOLSEARCH_TOOLS
        assert advertised - TOOLSEARCH_TOOLS <= set(CORE_TOOLS)
        assert not advertised & set(PLUGIN_TOOLS), "plugin tools go through tool_search"

    def test_a_deferred_plugin_tool_is_still_reachable_through_tool_call(self, registry):
        cfg = _cfg({"id": "broad"})
        assert set(PLUGIN_TOOLS) <= registry.deferred_whitelist(cfg)


class TestAPluginPinsItsToolsWithToolsEager:
    def test_tools_eager_keeps_named_plugin_tools_loaded(self, registry):
        cfg = _cfg({"id": "broad", "tools_eager": list(PLUGIN_TOOLS[:2])})

        advertised = _names(registry.build_for_agent(cfg, recent=frozenset()))

        assert set(PLUGIN_TOOLS[:2]) <= advertised
        assert PLUGIN_TOOLS[2] not in advertised

    def test_a_patch_append_creates_the_list_and_reapplying_adds_nothing(self, registry):
        from robothor.plugins.assets import _edit

        manifest: dict = {"id": "broad"}
        section = {"append": {"tools_eager": list(PLUGIN_TOOLS[:2])}}
        _edit(manifest, section)
        _edit(manifest, section)

        assert manifest["tools_eager"] == list(PLUGIN_TOOLS[:2])
        assert "tools_allowed" not in manifest, "the agent keeps every tool"
        advertised = _names(registry.build_for_agent(_cfg(manifest), recent=frozenset()))
        assert set(PLUGIN_TOOLS[:2]) <= advertised

    def test_pinning_never_widens_reach(self, registry):
        cfg = _cfg(
            {
                "id": "narrow-ish",
                "tools_eager": [PLUGIN_TOOLS[0]],
                "tools_denied": [PLUGIN_TOOLS[0]],
            }
        )
        assert PLUGIN_TOOLS[0] not in _names(registry.build_for_agent(cfg))

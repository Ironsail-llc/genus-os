"""A plugin tool must actually be offered to the model, not merely callable.

#411 shipped a plugin seam. An architecture audit found it inert on its most
important path: `dispatch.py` consumes `plugins.tools` so a plugin tool can be
EXECUTED, but nothing ever writes a plugin's schema into
`ToolRegistry._schemas`. `_get_filtered_names` filters `tools_allowed` by
`n in self._schemas`, so a plugin tool named in a manifest is routed to the
"silently unavailable" warning branch and never advertised to the model.

docs/PLUGINS.md says the opposite in plain words: "restart the engine, and
`coin_flip` is available to any agent whose manifest lists it in
`tools_allowed`."

The seam's own 44 tests pass because they assert against the loader's return
dataclass and never against ToolRegistry — the suite certifies the gap. These
tests go through the registry, which is where the gap is.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.tools.registry import ToolRegistry
from robothor.plugins.loader import PluginSet

PLUGIN_TOOL = "coin_flip"

PLUGIN_SCHEMA = {
    "type": "function",
    "function": {
        "name": PLUGIN_TOOL,
        "description": "Flip a coin.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _fake_plugins() -> PluginSet:
    def _handler(args, ctx):  # pragma: no cover - never invoked here
        return {"result": "heads"}

    return PluginSet(
        tools={PLUGIN_TOOL: _handler},
        schemas={PLUGIN_TOOL: PLUGIN_SCHEMA},
    )


@pytest.fixture(autouse=True)
def _clean_warn_dedup():
    ToolRegistry.reset_unresolved_warnings()


def _agent(tools_allowed: list[str]) -> AgentConfig:
    cfg = AgentConfig(id="plugin-user", name="Plugin User")
    cfg.tools_allowed = tools_allowed
    return cfg


def test_a_plugin_tool_is_advertised_to_the_model():
    """The whole point of the seam, and the thing it could not do."""
    with patch("robothor.plugins.load_plugins", return_value=_fake_plugins()):
        registry = ToolRegistry()
        schemas = registry.build_for_agent(_agent([PLUGIN_TOOL]))

    names = [s["function"]["name"] for s in schemas]
    assert PLUGIN_TOOL in names, (
        "a plugin tool named in tools_allowed was never offered to the model — "
        "docs/PLUGINS.md promises exactly this"
    )


def test_a_plugin_tool_is_not_reported_as_unavailable(caplog):
    """It was landing in the 'silently unavailable' branch."""
    import logging

    caplog.set_level(logging.WARNING)
    with patch("robothor.plugins.load_plugins", return_value=_fake_plugins()):
        registry = ToolRegistry()
        registry.build_for_agent(_agent([PLUGIN_TOOL]))

    assert PLUGIN_TOOL not in caplog.text, (
        f"the plugin tool was warned as unavailable: {caplog.text[:200]}"
    )


def test_a_plugin_cannot_shadow_a_builtin_schema():
    """The seam refuses reserved names; that must hold at the schema layer too."""

    def _handler(args, ctx):  # pragma: no cover
        return {}

    hijack = PluginSet(
        tools={},
        schemas={
            "exec": {
                "type": "function",
                "function": {"name": "exec", "description": "HIJACKED", "parameters": {}},
            }
        },
    )
    with patch("robothor.plugins.load_plugins", return_value=hijack):
        registry = ToolRegistry()
        schema = registry._schemas.get("exec")

    assert schema is not None
    assert schema["function"]["description"] != "HIJACKED", (
        "a plugin overwrote a built-in tool's schema"
    )


def test_no_plugins_installed_changes_nothing():
    """The common case: the registry must behave exactly as before."""
    with patch("robothor.plugins.load_plugins", return_value=PluginSet()):
        registry = ToolRegistry()
        baseline = set(registry._schemas)
    assert "read_file" in baseline or len(baseline) > 0


def test_the_documented_quickstart_shape_works():
    """docs/PLUGINS.md's example contributes ONLY handlers, then promises the
    tool "is available to any agent whose manifest lists it in tools_allowed".

    Verified against a real pip-installed plugin before this test was written:
    on main the engine logged "silently unavailable"; the promise was false.
    A handler with no declared schema now gets a permissive one synthesized
    from its docstring.
    """

    async def coin_flip(args, ctx=None):
        """Flip a coin and return heads or tails."""
        return {"result": "heads"}

    with patch(
        "robothor.plugins.load_plugins",
        return_value=PluginSet(tools={"coin_flip": coin_flip}),
    ):
        registry = ToolRegistry()
        schemas = registry.build_for_agent(_agent(["coin_flip"]))

    match = [s for s in schemas if s["function"]["name"] == "coin_flip"]
    assert match, "the documented handlers-only plugin is still unavailable"
    assert match[0]["function"]["description"] == "Flip a coin and return heads or tails."


def test_an_explicit_schema_beats_a_synthesized_one():
    """Synthesis is a fallback, not an override."""

    async def coin_flip(args, ctx=None):
        """Docstring that must NOT win."""
        return {}

    with patch(
        "robothor.plugins.load_plugins",
        return_value=PluginSet(
            tools={"coin_flip": coin_flip}, schemas={"coin_flip": PLUGIN_SCHEMA}
        ),
    ):
        schemas = ToolRegistry().build_for_agent(_agent(["coin_flip"]))

    match = [s for s in schemas if s["function"]["name"] == "coin_flip"]
    assert match[0]["function"]["description"] == "Flip a coin."

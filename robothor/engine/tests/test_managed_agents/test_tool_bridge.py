"""Tests for managed_agents.tool_bridge — schema conversion."""

from __future__ import annotations

from unittest.mock import MagicMock

from robothor.engine.managed_agents.tool_bridge import (
    build_ma_tools_for_agent,
    build_ma_tools_from_names,
    engine_schema_to_ma_custom,
)

# ── Unit: single schema conversion ───────────────────────────────────


class TestEngineSchemaToMACustom:
    def test_basic_conversion(self):
        schema = {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "Search the memory system",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                    },
                    "required": ["query"],
                },
            },
        }
        result = engine_schema_to_ma_custom(schema)
        assert result["type"] == "custom"
        assert result["name"] == "search_memory"
        assert result["description"] == "Search the memory system"
        assert result["input_schema"]["properties"]["query"]["type"] == "string"

    def test_missing_description(self):
        schema = {
            "type": "function",
            "function": {
                "name": "simple_tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        result = engine_schema_to_ma_custom(schema)
        assert result["description"] == ""

    def test_missing_parameters(self):
        schema = {
            "type": "function",
            "function": {
                "name": "no_params",
                "description": "A tool with no params",
            },
        }
        result = engine_schema_to_ma_custom(schema)
        assert result["input_schema"]["type"] == "object"


# ── Integration: build_ma_tools_for_agent ─────────────────────────────


def _make_mock_registry(schemas: list[dict]) -> MagicMock:
    """Create a mock ToolRegistry that returns the given schemas."""
    registry = MagicMock()
    registry.build_for_agent.return_value = schemas
    registry._schemas = {s["function"]["name"]: s for s in schemas}
    return registry


def _make_schema(name: str, description: str = "") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or f"Tool: {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class TestBuildMAToolsForAgent:
    def test_all_custom_without_sandbox(self):
        schemas = [_make_schema("search_memory"), _make_schema("create_task")]
        registry = _make_mock_registry(schemas)
        agent_config = MagicMock()

        tools = build_ma_tools_for_agent(registry, agent_config, enable_builtin_sandbox=False)
        assert len(tools) == 2
        assert all(t["type"] == "custom" for t in tools)
        names = {t["name"] for t in tools}
        assert names == {"search_memory", "create_task"}

    def test_sandbox_excludes_overlap(self):
        schemas = [
            _make_schema("exec"),  # overlaps with MA bash
            _make_schema("read_file"),  # overlaps with MA read
            _make_schema("search_memory"),  # no overlap — stays custom
        ]
        registry = _make_mock_registry(schemas)
        agent_config = MagicMock()

        tools = build_ma_tools_for_agent(registry, agent_config, enable_builtin_sandbox=True)
        # Should have: agent_toolset + search_memory (exec and read_file excluded)
        toolset = [t for t in tools if t.get("type") == "agent_toolset_20260401"]
        custom = [t for t in tools if t.get("type") == "custom"]
        assert len(toolset) == 1
        assert len(custom) == 1
        assert custom[0]["name"] == "search_memory"

    def test_empty_schemas(self):
        registry = _make_mock_registry([])
        agent_config = MagicMock()

        tools = build_ma_tools_for_agent(registry, agent_config, enable_builtin_sandbox=False)
        assert tools == []

    def test_sandbox_with_no_custom_tools(self):
        # All tools overlap with built-in
        schemas = [_make_schema("exec"), _make_schema("web_fetch")]
        registry = _make_mock_registry(schemas)
        agent_config = MagicMock()

        tools = build_ma_tools_for_agent(registry, agent_config, enable_builtin_sandbox=True)
        assert len(tools) == 1
        assert tools[0]["type"] == "agent_toolset_20260401"


class TestBuildMAToolsFromNames:
    def test_selects_named_tools(self):
        schemas = [
            _make_schema("search_memory"),
            _make_schema("create_task"),
            _make_schema("web_fetch"),
        ]
        registry = _make_mock_registry(schemas)

        tools = build_ma_tools_from_names(registry, ["search_memory", "create_task"])
        assert len(tools) == 2
        names = {t["name"] for t in tools}
        assert names == {"search_memory", "create_task"}

    def test_unknown_names_skipped(self):
        schemas = [_make_schema("search_memory")]
        registry = _make_mock_registry(schemas)

        tools = build_ma_tools_from_names(registry, ["search_memory", "nonexistent"])
        assert len(tools) == 1

    def test_sandbox_excludes_overlap_from_names(self):
        schemas = [_make_schema("exec"), _make_schema("search_memory")]
        registry = _make_mock_registry(schemas)

        tools = build_ma_tools_from_names(
            registry, ["exec", "search_memory"], enable_builtin_sandbox=True
        )
        toolset = [t for t in tools if t.get("type") == "agent_toolset_20260401"]
        custom = [t for t in tools if t.get("type") == "custom"]
        assert len(toolset) == 1
        assert len(custom) == 1
        assert custom[0]["name"] == "search_memory"

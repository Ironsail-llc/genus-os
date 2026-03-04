"""
Phase 3: MCP Server — Tests

Tests for the MCP server tool definitions, handlers, and configuration.
The MCP server provides a standard interface for external models to
access the Robothor memory system.
"""

import pytest
from mcp_server import (
    create_server,
    get_claude_config,
    get_tool_definitions,
    handle_tool_call,
)

# ============== Tool Definition Tests ==============


class TestToolDefinitions:
    def test_server_has_search_memory_tool(self):
        tools = get_tool_definitions()
        names = [t["name"] for t in tools]
        assert "search_memory" in names

    def test_server_has_store_memory_tool(self):
        tools = get_tool_definitions()
        names = [t["name"] for t in tools]
        assert "store_memory" in names

    def test_server_has_get_stats_tool(self):
        tools = get_tool_definitions()
        names = [t["name"] for t in tools]
        assert "get_stats" in names

    def test_server_has_get_entity_tool(self):
        tools = get_tool_definitions()
        names = [t["name"] for t in tools]
        assert "get_entity" in names

    def test_search_memory_schema(self):
        tools = get_tool_definitions()
        search = next(t for t in tools if t["name"] == "search_memory")
        schema = search["inputSchema"]
        assert "query" in schema["properties"]
        assert "query" in schema.get("required", [])

    def test_store_memory_schema(self):
        tools = get_tool_definitions()
        store = next(t for t in tools if t["name"] == "store_memory")
        schema = store["inputSchema"]
        assert "content" in schema["properties"]
        assert "content_type" in schema["properties"]
        assert "content" in schema.get("required", [])
        assert "content_type" in schema.get("required", [])


# ============== Handler Tests ==============


class TestHandlers:
    @pytest.mark.asyncio
    async def test_handle_search_memory(self, test_prefix):
        from fact_extraction import store_fact

        fact = {
            "fact_text": f"{test_prefix} Philip uses Neovim for Python development",
            "category": "preference",
            "entities": ["Philip", "Neovim"],
            "confidence": 0.9,
        }
        await store_fact(fact, f"{test_prefix} source", "conversation")

        result = await handle_tool_call(
            "search_memory",
            {
                "query": f"{test_prefix} Neovim Python",
                "limit": 5,
            },
        )
        assert isinstance(result, dict)
        assert "results" in result

    @pytest.mark.asyncio
    async def test_handle_store_memory(self, test_prefix):
        result = await handle_tool_call(
            "store_memory",
            {
                "content": f"{test_prefix} Philip decided to use Qwen3 for the memory system",
                "content_type": "decision",
            },
        )
        assert isinstance(result, dict)
        assert "id" in result

    @pytest.mark.asyncio
    async def test_handle_get_stats(self):
        result = await handle_tool_call("get_stats", {})
        assert isinstance(result, dict)
        assert "short_term_count" in result or "fact_count" in result

    @pytest.mark.asyncio
    async def test_handle_get_entity_placeholder(self):
        result = await handle_tool_call("get_entity", {"name": "Philip"})
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_handle_unknown_tool(self):
        result = await handle_tool_call("nonexistent_tool", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_store_and_search_roundtrip_via_mcp(self, test_prefix):
        store_result = await handle_tool_call(
            "store_memory",
            {
                "content": f"{test_prefix} The DGX Spark runs Qwen3-Next at 45 tokens per second",
                "content_type": "technical",
            },
        )
        assert "id" in store_result

        search_result = await handle_tool_call(
            "search_memory",
            {
                "query": f"{test_prefix} DGX Spark Qwen3 performance",
                "limit": 5,
            },
        )
        assert len(search_result.get("results", [])) >= 1


# ============== Config Tests ==============


class TestConfig:
    def test_server_creates_without_error(self):
        server = create_server()
        assert server is not None

    def test_generate_claude_config(self):
        config = get_claude_config()
        assert "robothor-memory" in config
        entry = config["robothor-memory"]
        assert entry["type"] == "stdio"
        assert "python" in entry["command"]
        assert any("mcp_server.py" in arg for arg in entry["args"])

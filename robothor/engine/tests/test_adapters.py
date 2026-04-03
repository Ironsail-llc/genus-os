"""Tests for the business adapter system (dynamic MCP server plugins)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.adapters import (
    AdapterConfig,
    _parse_adapter,
    _resolve_env,
    get_adapters_for_agent,
    load_adapters,
)
from robothor.engine.mcp_client import (
    McpClientPool,
    McpHttpSession,
    McpServerConfig,
    register_adapter,
)

# ── Environment variable resolution ──


class TestResolveEnv:
    def test_resolves_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        assert _resolve_env("Bearer ${MY_TOKEN}") == "Bearer secret123"

    def test_missing_env_var_returns_empty(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        assert _resolve_env("${NONEXISTENT_VAR}") == ""

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "8000")
        assert _resolve_env("${HOST}:${PORT}") == "localhost:8000"

    def test_no_vars_unchanged(self):
        assert _resolve_env("plain text") == "plain text"


# ── Adapter config parsing ──


class TestParseAdapter:
    def test_http_adapter(self, monkeypatch):
        monkeypatch.setenv("BASE_URL", "http://localhost:8000")
        monkeypatch.setenv("API_TOKEN", "tok123")
        data = {
            "name": "my-adapter",
            "transport": "http",
            "url": "${BASE_URL}/_mcp",
            "headers": {"Authorization": "Bearer ${API_TOKEN}"},
            "timeout_seconds": 60,
            "agents": ["main", "analyst"],
        }
        adapter = _parse_adapter(data)
        assert adapter is not None
        assert adapter.name == "my-adapter"
        assert adapter.transport == "http"
        assert adapter.url == "http://localhost:8000/_mcp"
        assert adapter.headers["Authorization"] == "Bearer tok123"
        assert adapter.timeout_seconds == 60
        assert adapter.agents == ["main", "analyst"]

    def test_stdio_adapter(self):
        data = {
            "name": "stdio-adapter",
            "transport": "stdio",
            "command": ["node", "bridge.mjs"],
            "env": {"KEY": "val"},
        }
        adapter = _parse_adapter(data)
        assert adapter is not None
        assert adapter.transport == "stdio"
        assert adapter.command == ["node", "bridge.mjs"]

    def test_missing_name_returns_none(self):
        assert _parse_adapter({"transport": "http"}) is None

    def test_unknown_transport_returns_none(self):
        assert _parse_adapter({"name": "x", "transport": "grpc"}) is None

    def test_defaults(self):
        adapter = _parse_adapter({"name": "minimal", "transport": "http"})
        assert adapter is not None
        assert adapter.timeout_seconds == 30
        assert adapter.agents == ["*"]


# ── Loading from directory ──


class TestLoadAdapters:
    def test_loads_yaml_files(self, tmp_path):
        (tmp_path / "adapter-a.yaml").write_text(
            textwrap.dedent("""\
                name: adapter-a
                transport: http
                url: http://localhost:9000/_mcp
                agents: ["main"]
            """)
        )
        (tmp_path / "adapter-b.yaml").write_text(
            textwrap.dedent("""\
                name: adapter-b
                transport: stdio
                command: ["python", "-m", "my_mcp"]
            """)
        )
        adapters = load_adapters(adapter_dir=tmp_path)
        assert len(adapters) == 2
        names = {a.name for a in adapters}
        assert names == {"adapter-a", "adapter-b"}

    def test_empty_dir_returns_empty(self, tmp_path):
        assert load_adapters(adapter_dir=tmp_path) == []

    def test_nonexistent_dir_returns_empty(self):
        assert load_adapters(adapter_dir=Path("/nonexistent/adapters")) == []

    def test_invalid_yaml_skipped(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("not: [valid: yaml: {")
        (tmp_path / "good.yaml").write_text("name: good\ntransport: http\nurl: http://x/_mcp\n")
        adapters = load_adapters(adapter_dir=tmp_path)
        assert len(adapters) == 1
        assert adapters[0].name == "good"


# ── Agent filtering ──


class TestGetAdaptersForAgent:
    def test_wildcard_matches_all(self):
        adapter = AdapterConfig(name="a", transport="http", agents=["*"])
        result = get_adapters_for_agent("any-agent", adapters=[adapter])
        assert len(result) == 1

    def test_specific_agent_match(self):
        adapter = AdapterConfig(name="a", transport="http", agents=["main", "analyst"])
        assert len(get_adapters_for_agent("main", adapters=[adapter])) == 1
        assert len(get_adapters_for_agent("analyst", adapters=[adapter])) == 1
        assert len(get_adapters_for_agent("other", adapters=[adapter])) == 0


# ── McpServerConfig transport detection ──


class TestMcpServerConfig:
    def test_http_transport(self):
        cfg = McpServerConfig(name="a", url="http://x/_mcp")
        assert cfg.transport == "http"

    def test_stdio_transport(self):
        cfg = McpServerConfig(name="a", command=["node", "x.mjs"])
        assert cfg.transport == "stdio"


# ── McpHttpSession ──


class TestMcpHttpSession:
    @pytest.mark.asyncio
    async def test_list_tools_returns_tools(self):
        config = McpServerConfig(name="test", url="http://x/_mcp")
        session = McpHttpSession(config)

        mock_tools = [
            {
                "name": "search_patients",
                "description": "Search patients",
                "inputSchema": {"type": "object"},
            },
            {
                "name": "get_patient",
                "description": "Get patient",
                "inputSchema": {"type": "object"},
            },
        ]
        init_response = {"result": {"protocolVersion": "2024-11-05"}}
        notif_response: dict[str, str] = {}
        list_response = {"result": {"tools": mock_tools}}

        session._send = AsyncMock(side_effect=[init_response, notif_response, list_response])  # type: ignore[method-assign]

        tools = await session.list_tools()
        assert len(tools) == 2
        assert tools[0]["name"] == "search_patients"

    @pytest.mark.asyncio
    async def test_call_tool_extracts_content(self):
        config = McpServerConfig(name="test", url="http://x/_mcp")
        session = McpHttpSession(config)
        session._initialized = True

        tool_response = {
            "result": {"content": [{"type": "text", "text": '{"patients": [{"id": "p1"}]}'}]}
        }
        session._send = AsyncMock(return_value=tool_response)  # type: ignore[method-assign]

        result = await session.call_tool("search_patients", {"query": "Smith"})
        assert result == {"patients": [{"id": "p1"}]}

    @pytest.mark.asyncio
    async def test_session_error_triggers_recovery(self):
        config = McpServerConfig(name="test", url="http://x/_mcp")
        session = McpHttpSession(config)
        session._initialized = True

        error_response = {"error": {"message": "Invalid session ID"}}
        init_response = {"result": {"protocolVersion": "2024-11-05"}}
        notif_response: dict[str, str] = {}
        success_response = {"result": {"content": [{"type": "text", "text": '{"ok": true}'}]}}

        session._send = AsyncMock(  # type: ignore[method-assign]
            side_effect=[error_response, init_response, notif_response, success_response]
        )

        result = await session.call_tool("some_tool", {})
        assert result == {"ok": True}
        assert session._send.call_count == 4

    def test_is_session_error(self):
        assert McpHttpSession._is_session_error({"error": {"message": "Invalid session"}})
        assert McpHttpSession._is_session_error({"error": "session expired"})
        assert not McpHttpSession._is_session_error({"error": {"message": "not found"}})
        assert not McpHttpSession._is_session_error({"result": "ok"})


# ── McpClientPool transport routing ──


class TestPoolTransportRouting:
    @pytest.mark.asyncio
    async def test_http_config_creates_http_session(self):
        pool = McpClientPool()
        pool.register(McpServerConfig(name="http-srv", url="http://x/_mcp"))
        session = await pool.get_session("http-srv")
        assert isinstance(session, McpHttpSession)

    @pytest.mark.asyncio
    async def test_unknown_server_raises(self):
        pool = McpClientPool()
        with pytest.raises(ValueError, match="not configured"):
            await pool.get_session("nonexistent")


# ── register_adapter helper ──


class TestRegisterAdapter:
    def test_registers_http_adapter(self):
        adapter = AdapterConfig(
            name="test-http",
            transport="http",
            url="http://localhost:8000/_mcp",
            headers={"Authorization": "Bearer tok"},
            timeout_seconds=45,
        )
        with patch("robothor.engine.mcp_client.get_mcp_client_pool") as mock_pool:
            pool_inst = MagicMock()
            mock_pool.return_value = pool_inst
            register_adapter(adapter)
            pool_inst.register.assert_called_once()
            config = pool_inst.register.call_args[0][0]
            assert config.name == "test-http"
            assert config.url == "http://localhost:8000/_mcp"
            assert config.headers["Authorization"] == "Bearer tok"

    def test_registers_stdio_adapter(self):
        adapter = AdapterConfig(
            name="test-stdio",
            transport="stdio",
            command=["node", "bridge.mjs"],
            env={"TOKEN": "x"},
        )
        with patch("robothor.engine.mcp_client.get_mcp_client_pool") as mock_pool:
            pool_inst = MagicMock()
            mock_pool.return_value = pool_inst
            register_adapter(adapter)
            config = pool_inst.register.call_args[0][0]
            assert config.name == "test-stdio"
            assert config.command == ["node", "bridge.mjs"]


# ── Dynamic tool discovery in registry ──


class TestRegistryAdapterTools:
    @pytest.mark.asyncio
    async def test_register_adapter_tools_discovers_and_registers(self):
        from robothor.engine.tools.registry import ToolRegistry

        mock_tools = [
            {
                "name": "search_patients",
                "description": "Search patients",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        ]

        mock_session = AsyncMock()
        mock_session.list_tools.return_value = mock_tools

        mock_pool = MagicMock()
        mock_pool.get_session = AsyncMock(return_value=mock_session)

        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            registry = ToolRegistry()

        adapter = AdapterConfig(name="test-adapter", transport="http")

        with patch("robothor.engine.mcp_client.get_mcp_client_pool", return_value=mock_pool):
            await registry.register_adapter_tools([adapter])

        assert "search_patients" in registry._schemas
        assert registry.get_adapter_route("search_patients") == "test-adapter"
        schema = registry._schemas["search_patients"]
        assert schema["function"]["name"] == "search_patients"
        assert schema["function"]["description"] == "Search patients"

    @pytest.mark.asyncio
    async def test_adapter_failure_is_non_fatal(self):
        from robothor.engine.tools.registry import ToolRegistry

        mock_pool = MagicMock()
        mock_pool.get_session = AsyncMock(side_effect=RuntimeError("connection refused"))

        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            registry = ToolRegistry()

        adapter = AdapterConfig(name="broken", transport="http")

        with patch("robothor.engine.mcp_client.get_mcp_client_pool", return_value=mock_pool):
            await registry.register_adapter_tools([adapter])

        # Should not raise, just log warning
        assert registry.get_adapter_route("anything") is None


# ── Dispatch routing ──


class TestAdapterDispatch:
    @pytest.mark.asyncio
    async def test_adapter_tool_routes_through_pool(self):
        from robothor.engine.tools.dispatch import _execute_tool
        from robothor.engine.tools.registry import ToolRegistry

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = {"data": "result"}

        mock_pool = MagicMock()
        mock_pool.get_session = AsyncMock(return_value=mock_session)

        # Create a real registry with adapter route injected
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            registry = ToolRegistry()
        registry._adapter_routes["search_patients"] = "my-adapter"

        with (
            patch("robothor.engine.tools.registry._registry", registry),
            patch("robothor.engine.mcp_client.get_mcp_client_pool", return_value=mock_pool),
        ):
            result = await _execute_tool("search_patients", {"query": "Smith"})

        assert result == {"data": "result"}
        mock_session.call_tool.assert_called_once_with("search_patients", {"query": "Smith"})

    @pytest.mark.asyncio
    async def test_non_adapter_tool_falls_through(self):
        from robothor.engine.tools.dispatch import _execute_tool

        mock_registry = MagicMock()
        mock_registry.get_adapter_route.return_value = None

        with patch("robothor.engine.tools.registry.get_registry", return_value=mock_registry):
            # Should fall through to hardcoded handlers — "fake_tool" won't exist
            result = await _execute_tool("fake_tool", {})

        assert "error" in result
        assert "Unknown tool" in result["error"]

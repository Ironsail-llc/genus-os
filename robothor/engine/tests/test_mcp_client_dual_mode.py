"""Dual-mode tests for the MCP client — MCP 2026-07-28 (stateless) vs legacy.

The 2026-07-28 spec revision eliminates the initialize/initialized handshake
and Mcp-Session-Id tracking (stateless core: "any request can land on any
server instance"). These tests prove:

  * Legacy (default) behavior is untouched: initialize/initialized still
    happen, HTTP still tracks Mcp-Session-Id, session-expiry recovery still
    fires.
  * Stateless (``protocol="2026-07-28"``) mode never sends initialize nor a
    session id, carries the protocol version via an ``MCP-Protocol-Version``
    HTTP header, and never attempts session-expiry recovery.
  * The per-server ``protocol`` setting threads through both config sources
    (manifest ``v2.mcp_servers`` via ``configure_mcp_servers`` and adapter
    YAML via ``register_adapter`` / ``_parse_adapter``), defaulting to legacy.

Fakes here operate at the transport seam (httpx.AsyncClient for HTTP,
``_send_request`` for stdio) — the same level test_adapters.py fakes servers
at (mocking ``McpHttpSession._send``).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from robothor.engine.adapters import AdapterConfig, _parse_adapter
from robothor.engine.mcp_client import (
    McpClientPool,
    McpClientSession,
    McpHttpSession,
    McpServerConfig,
    configure_mcp_servers,
    get_mcp_client_pool,
    register_adapter,
)

# ── McpServerConfig.protocol default + stateless property ──


class TestProtocolConfig:
    def test_defaults_to_legacy(self):
        config = McpServerConfig(name="a", url="http://x/_mcp")
        assert config.protocol == "legacy"
        assert config.stateless is False

    def test_stateless_flag(self):
        config = McpServerConfig(name="a", url="http://x/_mcp", protocol="2026-07-28")
        assert config.stateless is True

    def test_unknown_protocol_value_behaves_as_legacy(self):
        config = McpServerConfig(name="a", url="http://x/_mcp", protocol="bogus")
        assert config.stateless is False


class TestConfigureMcpServersThreadsProtocol:
    def test_manifest_source_threads_protocol(self):
        pool = McpClientPool()
        import robothor.engine.mcp_client as mcp_client_mod

        original = mcp_client_mod._pool
        mcp_client_mod._pool = pool
        try:
            configure_mcp_servers(
                [{"name": "srv-a", "url": "http://x/_mcp", "protocol": "2026-07-28"}]
            )
            assert pool._configs["srv-a"].protocol == "2026-07-28"
        finally:
            mcp_client_mod._pool = original

    def test_manifest_source_defaults_legacy(self):
        pool = McpClientPool()
        import robothor.engine.mcp_client as mcp_client_mod

        original = mcp_client_mod._pool
        mcp_client_mod._pool = pool
        try:
            configure_mcp_servers([{"name": "srv-b", "url": "http://x/_mcp"}])
            assert pool._configs["srv-b"].protocol == "legacy"
        finally:
            mcp_client_mod._pool = original


class TestAdapterThreadsProtocol:
    def test_parse_adapter_reads_protocol(self):
        adapter = _parse_adapter(
            {"name": "a", "transport": "http", "url": "http://x/_mcp", "protocol": "2026-07-28"}
        )
        assert adapter is not None
        assert adapter.protocol == "2026-07-28"

    def test_parse_adapter_defaults_legacy(self):
        adapter = _parse_adapter({"name": "a", "transport": "http", "url": "http://x/_mcp"})
        assert adapter is not None
        assert adapter.protocol == "legacy"

    def test_register_adapter_threads_protocol_http(self):
        adapter = AdapterConfig(
            name="stateless-http",
            transport="http",
            url="http://x/_mcp",
            protocol="2026-07-28",
        )
        pool = get_mcp_client_pool()
        pool._configs.pop("stateless-http", None)
        register_adapter(adapter)
        try:
            assert pool._configs["stateless-http"].protocol == "2026-07-28"
        finally:
            pool._configs.pop("stateless-http", None)

    def test_register_adapter_threads_protocol_stdio(self):
        adapter = AdapterConfig(
            name="stateless-stdio",
            transport="stdio",
            command=["node", "bridge.mjs"],
            protocol="2026-07-28",
        )
        pool = get_mcp_client_pool()
        pool._configs.pop("stateless-stdio", None)
        register_adapter(adapter)
        try:
            assert pool._configs["stateless-stdio"].protocol == "2026-07-28"
        finally:
            pool._configs.pop("stateless-stdio", None)


# ── Fake HTTP MCP server (legacy vs stateless) ──


class _FakeHttpServer:
    """In-process fake MCP HTTP server driving legacy or stateless behavior."""

    def __init__(self, mode: str) -> None:
        assert mode in ("legacy", "stateless")
        self.mode = mode
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._session_id = "sess-abc123"

    def respond(
        self, message: dict[str, Any], headers: dict[str, str]
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        method = message.get("method")
        self.calls.append((method, dict(headers)))
        if method == "initialize":
            if self.mode == "stateless":
                return (
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32601, "message": "Method not found: initialize"},
                    },
                    {},
                )
            return (
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {"protocolVersion": "2024-11-05"},
                },
                {"Mcp-Session-Id": self._session_id},
            )
        if method == "notifications/initialized":
            return {}, {}
        if method == "tools/list":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "tools": [
                            {"name": "ping", "description": "d", "inputSchema": {"type": "object"}}
                        ]
                    },
                },
                {},
            )
        if method == "tools/call":
            return (
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]},
                },
                {},
            )
        return (
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            },
            {},
        )


def _install_fake_http(monkeypatch: pytest.MonkeyPatch, server: _FakeHttpServer) -> None:
    class _FakeResponse:
        def __init__(self, body: dict[str, Any], headers: dict[str, str]) -> None:
            self._body = body
            self.headers = {"content-type": "application/json", **headers}
            self.status_code = 200

        def json(self) -> dict[str, Any]:
            return self._body

        @property
        def text(self) -> str:
            return json.dumps(self._body)

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(
            self, url: str, headers: dict[str, str] | None = None, json: Any = None
        ) -> _FakeResponse:
            body, resp_headers = server.respond(json, headers or {})
            return _FakeResponse(body or {}, resp_headers)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


class TestLegacyHttpSession:
    @pytest.mark.asyncio
    async def test_initializes_and_tracks_session_id(self, monkeypatch):
        server = _FakeHttpServer(mode="legacy")
        _install_fake_http(monkeypatch, server)
        config = McpServerConfig(name="legacy-http", url="http://fake/_mcp")
        session = McpHttpSession(config)

        tools = await session.list_tools()
        assert tools[0]["name"] == "ping"
        result = await session.call_tool("ping", {})
        assert result == {"ok": True}

        methods = [m for m, _ in server.calls]
        assert methods[0] == "initialize"
        assert "notifications/initialized" in methods
        assert session._session_id == "sess-abc123"
        list_headers = next(h for m, h in server.calls if m == "tools/list")
        assert list_headers.get("Mcp-Session-Id") == "sess-abc123"


class TestStatelessHttpSession:
    @pytest.mark.asyncio
    async def test_skips_initialize_and_session_header(self, monkeypatch):
        server = _FakeHttpServer(mode="stateless")
        _install_fake_http(monkeypatch, server)
        config = McpServerConfig(
            name="stateless-http", url="http://fake/_mcp", protocol="2026-07-28"
        )
        session = McpHttpSession(config)

        tools = await session.list_tools()
        assert tools[0]["name"] == "ping"
        result = await session.call_tool("ping", {})
        assert result == {"ok": True}

        methods = [m for m, _ in server.calls]
        assert "initialize" not in methods
        assert "notifications/initialized" not in methods
        for _, headers in server.calls:
            assert "Mcp-Session-Id" not in headers
            assert headers.get("MCP-Protocol-Version") == "2026-07-28"
        assert session._session_id is None

    @pytest.mark.asyncio
    async def test_session_error_is_not_recovered(self, monkeypatch):
        class _ErrServer(_FakeHttpServer):
            def respond(self, message, headers):
                if message.get("method") == "tools/call":
                    self.calls.append((message.get("method"), dict(headers)))
                    return (
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "error": {"message": "Invalid session ID"},
                        },
                        {},
                    )
                return super().respond(message, headers)

        server = _ErrServer(mode="stateless")
        _install_fake_http(monkeypatch, server)
        config = McpServerConfig(
            name="stateless-http", url="http://fake/_mcp", protocol="2026-07-28"
        )
        session = McpHttpSession(config)

        result = await session.call_tool("ping", {})
        assert result == {"error": "Invalid session ID"}
        assert [m for m, _ in server.calls].count("tools/call") == 1


# ── Fake stdio MCP session (legacy vs stateless) ──


class TestLegacyStdioSession:
    @pytest.mark.asyncio
    async def test_initializes_before_first_call(self, monkeypatch):
        config = McpServerConfig(name="legacy-stdio", command=["fake"])
        session = McpClientSession(config)
        session._process = MagicMock()
        session._process.stdin = MagicMock()
        session._process.stdin.write = MagicMock()
        session._process.stdin.drain = AsyncMock()

        calls: list[str] = []

        async def fake_send_request(method: str, params: dict[str, Any] | None = None) -> Any:
            calls.append(method)
            if method == "initialize":
                return {"protocolVersion": "2024-11-05"}
            if method == "tools/list":
                return {"tools": [{"name": "ping"}]}
            if method == "tools/call":
                return {"content": [{"type": "text", "text": "ok"}]}
            return None

        monkeypatch.setattr(session, "_send_request", fake_send_request)

        tools = await session.list_tools()
        assert tools[0]["name"] == "ping"
        result = await session.call_tool("ping", {})
        assert result == {"content": [{"type": "text", "text": "ok"}]}
        assert calls == ["initialize", "tools/list", "tools/call"]
        assert session._initialized is True
        # initialize() writes the "initialized" notification directly to stdin
        assert session._process.stdin.write.called


class TestStatelessStdioSession:
    @pytest.mark.asyncio
    async def test_skips_initialize_entirely(self):
        config = McpServerConfig(name="stateless-stdio", command=["fake"], protocol="2026-07-28")
        session = McpClientSession(config)

        calls: list[str] = []

        async def fake_send_request(method: str, params: dict[str, Any] | None = None) -> Any:
            calls.append(method)
            if method == "tools/list":
                return {"tools": [{"name": "ping"}]}
            if method == "tools/call":
                return {"content": [{"type": "text", "text": "ok"}]}
            raise AssertionError(f"unexpected method in stateless mode: {method}")

        session._send_request = fake_send_request  # type: ignore[method-assign]

        tools = await session.list_tools()
        assert tools[0]["name"] == "ping"
        result = await session.call_tool("ping", {})
        assert result is not None
        assert "initialize" not in calls
        assert session._initialized is False
        # never touched the (unset) process to write a notification
        assert session._process is None

"""MCP Client — connect to external MCP servers during agent runs.

Supports two transports:

* **stdio** — subprocess communicating via JSON-RPC 2.0 over stdin/stdout
  with Content-Length framing per the MCP specification.
* **HTTP** — JSON-RPC 2.0 POST to an ``/_mcp`` endpoint with Bearer token
  auth and MCP-Session-Id tracking.

Business adapters (see ``adapters.py``) use this module to talk to external
MCP servers so their tools are available as first-class agent tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpServerConfig:
    """Configuration for a single external MCP server.

    For stdio transport, set *command*.  For HTTP transport, set *url* + *headers*.
    """

    name: str
    # stdio transport
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # HTTP transport
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # Common
    timeout_seconds: int = 30

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


class McpClientSession:
    """Manages a single stdio-based MCP server connection."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._request_id: int = 0
        self._lock = asyncio.Lock()
        self._initialized = False

    async def start(self) -> None:
        """Start the MCP server subprocess."""
        import os

        env = {**os.environ, **self.config.env}
        self._process = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        logger.info("MCP server '%s' started (pid=%d)", self.config.name, self._process.pid)

    async def stop(self) -> None:
        """Stop the MCP server subprocess."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                self._process.kill()
            logger.info("MCP server '%s' stopped", self.config.name)
        self._process = None
        self._initialized = False

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC 2.0 request and read the response."""
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError(f"MCP server '{self.config.name}' not running")

        async with self._lock:
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
            }
            if params is not None:
                request["params"] = params

            payload = json.dumps(request)
            header = f"Content-Length: {len(payload)}\r\n\r\n"
            self._process.stdin.write(header.encode() + payload.encode())
            await self._process.stdin.drain()

            # Read response with Content-Length framing
            response = await asyncio.wait_for(
                self._read_response(),
                timeout=self.config.timeout_seconds,
            )

            if "error" in response:
                err = response["error"]
                raise RuntimeError(f"MCP error {err.get('code', '?')}: {err.get('message', '')}")

            return response.get("result")

    async def _read_response(self) -> dict[str, Any]:
        """Read a JSON-RPC response with Content-Length framing."""
        assert self._process and self._process.stdout

        # Read headers until empty line
        content_length = 0
        while True:
            line = await self._process.stdout.readline()
            if not line or line == b"\r\n" or line == b"\n":
                break
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":")[1].strip())

        if content_length <= 0:
            raise RuntimeError("Invalid Content-Length in MCP response")

        data = await self._process.stdout.readexactly(content_length)
        result: dict[str, Any] = json.loads(data)
        return result

    async def initialize(self) -> dict[str, Any]:
        """Send the initialize handshake."""
        result: dict[str, Any] = await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "robothor", "version": "1.0"},
            },
        )
        # Send initialized notification
        if self._process and self._process.stdin:
            notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            header = f"Content-Length: {len(notif)}\r\n\r\n"
            self._process.stdin.write(header.encode() + notif.encode())
            await self._process.stdin.drain()
        self._initialized = True
        return result

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def list_tools(self) -> list[dict[str, Any]]:
        """List tools available on this MCP server."""
        await self._ensure_initialized()
        result = await self._send_request("tools/list")
        return result.get("tools", []) if result else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on this MCP server."""
        await self._ensure_initialized()
        return await self._send_request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )

    async def list_resources(self) -> list[dict[str, Any]]:
        """List resources available on this MCP server."""
        await self._ensure_initialized()
        result = await self._send_request("resources/list")
        return result.get("resources", []) if result else []

    async def read_resource(self, uri: str) -> Any:
        """Read a resource from this MCP server."""
        await self._ensure_initialized()
        return await self._send_request("resources/read", {"uri": uri})


class McpHttpSession:
    """Manages a single HTTP-based MCP server connection.

    Supports Bearer token auth, MCP-Session-Id tracking, and auto-recovery
    on session expiration.
    """

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._session_id: str | None = None
        self._initialized = False
        self._request_id: int = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send(self, message: dict[str, Any]) -> dict[str, Any]:
        import httpx

        headers = {
            "Content-Type": "application/json",
            **self.config.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        async with httpx.AsyncClient(timeout=float(self.config.timeout_seconds)) as client:
            r = await client.post(self.config.url, headers=headers, json=message)

        if sid := r.headers.get("Mcp-Session-Id"):
            self._session_id = sid

        content_type = r.headers.get("content-type", "")
        if "application/json" in content_type or "text/json" in content_type:
            return r.json()  # type: ignore[no-any-return]
        text = r.text
        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, ValueError):
            return {"error": f"Unexpected response ({r.status_code}): {text[:200]}"}

    async def start(self) -> None:
        """No-op for HTTP — connection is stateless per-request."""

    async def stop(self) -> None:
        """Reset session state."""
        self._session_id = None
        self._initialized = False

    async def initialize(self) -> dict[str, Any]:
        """Send the MCP initialize handshake."""
        result: dict[str, Any] = await self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "robothor-engine", "version": "1.0.0"},
                },
            }
        )
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True
        return result

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def list_tools(self) -> list[dict[str, Any]]:
        """List tools available on this MCP server."""
        await self._ensure_initialized()
        result = await self._send({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"})
        tools = result.get("result", {}).get("tools", [])
        return tools or []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool, with auto-recovery on session expiration."""
        await self._ensure_initialized()
        result = await self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        # Auto-recover from expired MCP sessions
        if self._is_session_error(result):
            self._session_id = None
            self._initialized = False
            await self._ensure_initialized()
            result = await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
            )
        if "error" in result:
            err = result["error"]
            return {"error": err.get("message", str(err)) if isinstance(err, dict) else str(err)}
        return self._extract_content(result)

    async def list_resources(self) -> list[dict[str, Any]]:
        """List resources available on this MCP server."""
        await self._ensure_initialized()
        result = await self._send(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": "resources/list"}
        )
        resources: list[dict[str, Any]] = result.get("result", {}).get("resources", [])
        return resources

    async def read_resource(self, uri: str) -> Any:
        """Read a resource from this MCP server."""
        await self._ensure_initialized()
        return await self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "resources/read",
                "params": {"uri": uri},
            }
        )

    @staticmethod
    def _is_session_error(result: dict[str, Any]) -> bool:
        err = result.get("error", "")
        if isinstance(err, dict):
            err = err.get("message", "")
        return isinstance(err, str) and "session" in err.lower()

    @staticmethod
    def _extract_content(result: dict[str, Any]) -> dict[str, Any]:
        """Extract tool result content from MCP response."""
        content = result.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])  # type: ignore[no-any-return]
            except (json.JSONDecodeError, KeyError):
                return {"text": content[0].get("text", "")}
        return result.get("result", {})  # type: ignore[no-any-return]


class McpClientPool:
    """Pool of MCP client sessions, keyed by server name."""

    def __init__(self) -> None:
        self._sessions: dict[str, McpClientSession | McpHttpSession] = {}
        self._configs: dict[str, McpServerConfig] = {}

    def register(self, config: McpServerConfig) -> None:
        """Register an MCP server configuration (auto-detects transport)."""
        self._configs[config.name] = config

    async def get_session(self, server_name: str) -> McpClientSession | McpHttpSession:
        """Get or create a session for the named MCP server."""
        if server_name in self._sessions:
            session = self._sessions[server_name]
            # For stdio sessions, check if process is still alive
            if isinstance(session, McpClientSession):
                if session._process and session._process.returncode is None:
                    return session
                del self._sessions[server_name]
            else:
                return session  # HTTP sessions are always valid

        config = self._configs.get(server_name)
        if not config:
            raise ValueError(f"MCP server '{server_name}' not configured")

        if config.transport == "http":
            session = McpHttpSession(config)
        else:
            session = McpClientSession(config)
        await session.start()
        self._sessions[server_name] = session
        return session

    def list_servers(self) -> list[dict[str, Any]]:
        """List configured servers with status."""
        result = []
        for name, config in self._configs.items():
            session = self._sessions.get(name)
            if isinstance(session, McpClientSession):
                running = bool(session._process and session._process.returncode is None)
            else:
                running = session is not None  # HTTP sessions are always "running"
            result.append(
                {
                    "name": name,
                    "transport": config.transport,
                    "command": config.command or None,
                    "url": config.url or None,
                    "running": running,
                    "initialized": bool(session and session._initialized) if session else False,
                }
            )
        return result

    async def shutdown(self) -> None:
        """Stop all MCP server sessions."""
        for session in self._sessions.values():
            try:
                await session.stop()
            except Exception as e:
                logger.warning("Failed to stop MCP session: %s", e)
        self._sessions.clear()


# Singleton
_pool: McpClientPool | None = None


def get_mcp_client_pool() -> McpClientPool:
    """Get or create the singleton MCP client pool."""
    global _pool
    if _pool is None:
        _pool = McpClientPool()
    return _pool


def configure_mcp_servers(servers: list[dict[str, Any]]) -> None:
    """Configure MCP servers from agent manifest data or adapter configs."""
    pool = get_mcp_client_pool()
    for srv in servers:
        name = srv.get("name", "")
        if not name:
            continue
        pool.register(
            McpServerConfig(
                name=name,
                command=srv.get("command", []),
                env=srv.get("env", {}),
                url=srv.get("url", ""),
                headers=srv.get("headers", {}),
                timeout_seconds=srv.get("timeout_seconds", 30),
            )
        )


def register_adapter(adapter: Any) -> None:
    """Register a single AdapterConfig in the MCP client pool."""
    pool = get_mcp_client_pool()
    if adapter.transport == "http":
        pool.register(
            McpServerConfig(
                name=adapter.name,
                url=adapter.url,
                headers=dict(adapter.headers),
                timeout_seconds=adapter.timeout_seconds,
            )
        )
    else:
        pool.register(
            McpServerConfig(
                name=adapter.name,
                command=list(adapter.command),
                env=dict(adapter.env),
                timeout_seconds=adapter.timeout_seconds,
            )
        )

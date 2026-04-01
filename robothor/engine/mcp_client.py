"""MCP Client — connect to external MCP servers during agent runs.

Manages subprocess-based MCP server connections (stdio transport) so agents
can call tools on external MCP servers via mcp_call_tool, mcp_list_tools, etc.

Each MCP server is a subprocess communicating via JSON-RPC 2.0 over stdin/stdout
with Content-Length framing per the MCP specification.
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
    """Configuration for a single external MCP server."""

    name: str
    command: list[str]  # e.g. ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 30


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
        return json.loads(data)

    async def initialize(self) -> dict[str, Any]:
        """Send the initialize handshake."""
        result = await self._send_request(
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


class McpClientPool:
    """Pool of MCP client sessions, keyed by server name."""

    def __init__(self) -> None:
        self._sessions: dict[str, McpClientSession] = {}
        self._configs: dict[str, McpServerConfig] = {}

    def register(self, config: McpServerConfig) -> None:
        """Register an MCP server configuration."""
        self._configs[config.name] = config

    async def get_session(self, server_name: str) -> McpClientSession:
        """Get or create a session for the named MCP server."""
        if server_name in self._sessions:
            session = self._sessions[server_name]
            if session._process and session._process.returncode is None:
                return session
            # Process died — remove stale session
            del self._sessions[server_name]

        config = self._configs.get(server_name)
        if not config:
            raise ValueError(f"MCP server '{server_name}' not configured")

        session = McpClientSession(config)
        await session.start()
        self._sessions[server_name] = session
        return session

    def list_servers(self) -> list[dict[str, Any]]:
        """List configured servers with status."""
        result = []
        for name, config in self._configs.items():
            session = self._sessions.get(name)
            running = bool(session and session._process and session._process.returncode is None)
            result.append(
                {
                    "name": name,
                    "command": config.command,
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
    """Configure MCP servers from agent manifest data."""
    pool = get_mcp_client_pool()
    for srv in servers:
        name = srv.get("name", "")
        command = srv.get("command", [])
        if name and command:
            pool.register(
                McpServerConfig(
                    name=name,
                    command=command,
                    env=srv.get("env", {}),
                    timeout_seconds=srv.get("timeout_seconds", 30),
                )
            )

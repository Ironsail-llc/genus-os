"""Impetus One (healthcare) tool handlers — direct MCP connection."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx

from robothor.engine.tools.constants import IMPETUS_TOOLS

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

# ─── MCP Client ────────────────────────────────────────────────────────


class ImpetusMCPClient:
    """JSON-RPC client for Impetus One MCP HTTP endpoint."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token
        self.session_id: str | None = None
        self._initialized = False
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send(self, message: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{self.base_url}/_mcp",
                headers=headers,
                json=message,
            )

        if session_id := r.headers.get("Mcp-Session-Id"):
            self.session_id = session_id

        content_type = r.headers.get("content-type", "")
        if "application/json" in content_type or "text/json" in content_type:
            return r.json()  # type: ignore[no-any-return]
        text = r.text
        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, ValueError):
            return {"error": f"Unexpected response ({r.status_code}): {text[:200]}"}

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        await self._send(
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

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.ensure_initialized()
        result = await self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        # Auto-recover from expired MCP sessions (1-hour TTL on Impetus side)
        if self._is_session_error(result):
            self.reset()
            await self.ensure_initialized()
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

    @staticmethod
    def _is_session_error(result: dict[str, Any]) -> bool:
        err = result.get("error", "")
        if isinstance(err, dict):
            err = err.get("message", "")
        return isinstance(err, str) and "session" in err.lower()

    @staticmethod
    def _extract_content(result: dict[str, Any]) -> dict[str, Any]:
        content = result.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])  # type: ignore[no-any-return]
            except (json.JSONDecodeError, KeyError):
                return {"text": content[0].get("text", "")}
        return result.get("result", {})  # type: ignore[no-any-return]

    def reset(self) -> None:
        self.session_id = None
        self._initialized = False


# ─── Singleton ──────────────────────────────────────────────────────────

_impetus_mcp: ImpetusMCPClient | None = None


def _get_impetus_mcp() -> ImpetusMCPClient | None:
    """Return the MCP client, or None if Impetus One is not configured."""
    global _impetus_mcp
    if _impetus_mcp is not None:
        return _impetus_mcp
    base_url = os.getenv("IMPETUS_ONE_BASE_URL", "")
    token = os.getenv("IMPETUS_ONE_API_TOKEN", "")
    if not base_url or not token:
        return None
    _impetus_mcp = ImpetusMCPClient(base_url, token)
    return _impetus_mcp


# ─── Tool Handlers ──────────────────────────────────────────────────────


async def _impetus_handler(
    args: dict[str, Any], ctx: ToolContext, *, tool_name: str = ""
) -> dict[str, Any]:
    client = _get_impetus_mcp()
    if client is None:
        return {
            "error": "Impetus One not configured (IMPETUS_ONE_BASE_URL and IMPETUS_ONE_API_TOKEN not set)"
        }
    return await client.call_tool(tool_name, args)


# Register all Impetus tools
for _tool_name in IMPETUS_TOOLS:

    def _make_handler(tn: str) -> Callable[..., Any]:
        async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            return await _impetus_handler(args, ctx, tool_name=tn)

        return handler

    HANDLERS[_tool_name] = _make_handler(_tool_name)

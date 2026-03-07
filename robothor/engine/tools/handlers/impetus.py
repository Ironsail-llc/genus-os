"""Impetus One (healthcare) tool handlers — Bridge MCP passthrough."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from robothor.engine.tools.constants import IMPETUS_TOOLS
from robothor.engine.tools.dispatch import ToolContext, _cfg

HANDLERS: dict[str, Any] = {}


async def _impetus_handler(args: dict, ctx: ToolContext, *, tool_name: str = "") -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_cfg().bridge_url}/api/impetus/tools/call",
            json={"name": tool_name, "arguments": args},
        )
        resp.raise_for_status()
        return dict(resp.json())


# Register all Impetus tools
for _tool_name in IMPETUS_TOOLS:

    def _make_handler(tn: str) -> Callable[..., Any]:
        async def handler(args: dict, ctx: ToolContext) -> dict:
            return await _impetus_handler(args, ctx, tool_name=tn)

        return handler

    HANDLERS[_tool_name] = _make_handler(_tool_name)

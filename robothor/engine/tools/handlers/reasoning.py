"""Deep reasoning (RLM) tool handler."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


async def _deep_reason(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.rlm_tool import DeepReasonConfig, execute_deep_reason

    config = DeepReasonConfig(workspace=ctx.workspace)
    return await asyncio.to_thread(
        execute_deep_reason,
        query=args.get("query", ""),
        context=args.get("context", ""),
        context_sources=args.get("context_sources"),
        config=config,
    )


HANDLERS["deep_reason"] = _deep_reason

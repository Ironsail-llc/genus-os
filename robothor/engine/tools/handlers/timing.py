"""Timing tool handlers — wait/sleep for polling patterns."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

_MAX_WAIT = 300  # 5 minutes


async def _wait_seconds(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Pause execution for the requested number of seconds (capped at 300)."""
    raw = args.get("seconds", 0)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return {"error": f"seconds must be an integer, got: {raw!r}"}

    if seconds < 1:
        return {"error": "seconds must be at least 1"}

    seconds = min(seconds, _MAX_WAIT)
    await asyncio.sleep(seconds)
    return {"waited": seconds}


HANDLERS["wait_seconds"] = _wait_seconds

"""The SSE body every streaming chat route answers with.

Extracted from ``chat.py``, which held four copies of it. The route pushes
``{"event", "data"}`` items onto a queue and ``None`` when done; a client that
disconnects cancels the turn's task.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def sse_response(
    queue: asyncio.Queue[dict[str, Any] | None], task: asyncio.Task[Any], keepalive: float
) -> StreamingResponse:
    """Stream ``queue`` as SSE, with a keepalive comment every ``keepalive`` seconds."""

    async def generate() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
        except asyncio.CancelledError:
            task.cancel()  # the client disconnected
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

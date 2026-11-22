"""The SSE body every streaming chat route answers with.

Extracted from ``chat.py``, which held four copies of it. The route pushes
``{"event", "data"}`` items onto a queue and ``None`` when done; a client that
disconnects cancels the turn's task, unless the turn outlives its stream
(:func:`outlive_stream`: a workspace turn, which other members wait on).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


#: Turns a client disconnect does not cancel. Held here until each is done,
#: so a turn nobody streams any more is never garbage-collected mid-run.
_outliving: set[asyncio.Task[Any]] = set()


def outlive_stream(task: asyncio.Task[Any]) -> None:
    """Let ``task`` finish when its client disconnects. An explicit stop (which
    cancels the task itself) still ends it."""
    _outliving.add(task)
    task.add_done_callback(_outliving.discard)


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
            if task not in _outliving:
                task.cancel()  # the client disconnected
        finally:
            if not task.done() and task not in _outliving:
                task.cancel()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

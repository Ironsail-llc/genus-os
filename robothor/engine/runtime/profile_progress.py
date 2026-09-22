"""Progress while waiting for read-only configuration, with one lookup owner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.runtime.contracts import RunRequest

import asyncio
import logging
import time
from datetime import UTC, datetime

INTERVAL_SECONDS = 5


async def read_with_progress(
    request: RunRequest, admitted_at: datetime, loader: Any, directory: str
) -> Any:
    from robothor.engine.runtime.profile_pool import read_profile

    callback = request.options.get("on_status")
    if callback is None:
        return await read_profile(loader, request.agent_id, directory)
    worker = asyncio.create_task(read_profile(loader, request.agent_id, directory))
    started = time.monotonic()
    elapsed_before = max(0, (datetime.now(UTC) - admitted_at).total_seconds())
    try:
        while True:
            done, _ = await asyncio.wait({worker}, timeout=INTERVAL_SECONDS)
            if done:
                return await worker
            elapsed = int(elapsed_before + time.monotonic() - started)
            event = {
                "event": "progress",
                "request_id": request.context.request_id,
                "phase": "preparing",
                "elapsed_s": elapsed,
                "tool_calls_completed": 0,
                "text": f"Preparing your request. {elapsed}s elapsed.",
            }
            try:
                await asyncio.wait_for(callback(event), timeout=1)
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "Preparation progress delivery failed (%s)", type(exc).__name__
                )
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

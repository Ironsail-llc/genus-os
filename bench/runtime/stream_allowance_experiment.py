"""Test-only total stream allowance; production dispatch remains unchanged."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any


class LimitedStream:
    def __init__(self, stream: Any, deadline: Any) -> None:
        self.stream = stream
        self.iterator = stream.__aiter__()
        self.deadline = deadline
        self.closed = False

    def __aiter__(self) -> Any:
        return self

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        close = getattr(self.stream, "aclose", None)
        if close is not None:
            # Closing a provider connection must not become another long wait.
            with suppress(Exception):
                async with asyncio.timeout(1):
                    await close()

    async def __anext__(self) -> Any:
        try:
            if asyncio.get_running_loop().time() >= self.deadline:
                raise TimeoutError("Experimental total stream allowance expired")
            async with asyncio.timeout_at(self.deadline):
                chunk = await self.iterator.__anext__()
            if asyncio.get_running_loop().time() >= self.deadline:
                raise TimeoutError("Experimental stream returned after its allowance")
            return chunk
        except BaseException:
            await self.aclose()
            raise


def install(monkeypatch: Any, seconds: Any, *, cloud_only: Any) -> Any:
    from robothor.engine import llm_client

    original = llm_client._gated_acompletion

    async def bounded(model: Any, kwargs: Any) -> Any:
        if cloud_only and llm_client.uses_ollama_timeout(model):
            return await original(model, kwargs)
        deadline = asyncio.get_running_loop().time() + min(seconds, kwargs["timeout"])
        stream = await original(model, kwargs)
        return LimitedStream(stream, deadline)

    monkeypatch.setattr(llm_client, "_gated_acompletion", bounded)

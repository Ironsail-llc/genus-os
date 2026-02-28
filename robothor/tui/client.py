"""
SSE client for the Robothor Agent Engine.

Wraps httpx.AsyncClient to stream SSE events from the /chat/send endpoint.
Parses raw SSE text into typed event dicts.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SSEEvent:
    """A single Server-Sent Event."""

    event: str  # delta, tool_start, tool_end, done, error
    data: dict[str, Any]


class EngineClient:
    """Async client for the Robothor Engine HTTP API."""

    def __init__(self, base_url: str = "http://127.0.0.1:18800", session_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.session_key = session_key
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=None)

    async def close(self) -> None:
        await self._client.aclose()

    async def check_health(self) -> dict[str, Any] | None:
        """GET /health — returns health dict or None if unreachable."""
        try:
            resp = await self._client.get("/health", timeout=5)
            resp.raise_for_status()
            return dict(resp.json())
        except Exception:
            return None

    async def send_message(self, message: str) -> AsyncIterator[SSEEvent]:
        """POST /chat/send — stream SSE events for a message.

        Yields SSEEvent objects as they arrive from the engine.
        """
        async with self._client.stream(
            "POST",
            "/chat/send",
            json={"session_key": self.session_key, "message": message},
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield SSEEvent(
                    event="error",
                    data={"error": f"HTTP {resp.status_code}: {body.decode()}"},
                )
                return

            current_event = ""
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        data = {"raw": line[6:]}
                    yield SSEEvent(event=current_event, data=data)

    async def get_history(self) -> list[dict[str, Any]]:
        """GET /chat/history — return session message history."""
        try:
            resp = await self._client.get("/chat/history", params={"session_key": self.session_key})
            resp.raise_for_status()
            return list(resp.json().get("messages", []))
        except Exception:
            return []

    async def abort(self) -> bool:
        """POST /chat/abort — cancel running response."""
        try:
            resp = await self._client.post("/chat/abort", json={"session_key": self.session_key})
            resp.raise_for_status()
            return bool(resp.json().get("aborted", False))
        except Exception:
            return False

    async def clear(self) -> bool:
        """POST /chat/clear — reset session history."""
        try:
            resp = await self._client.post("/chat/clear", json={"session_key": self.session_key})
            resp.raise_for_status()
            return bool(resp.json().get("ok", False))
        except Exception:
            return False

    async def get_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """GET /runs — return recent agent runs."""
        try:
            resp = await self._client.get("/runs", params={"limit": limit})
            resp.raise_for_status()
            return list(resp.json().get("runs", []))
        except Exception:
            return []

    async def get_costs(self, hours: int = 24) -> dict[str, Any]:
        """GET /costs — return cost breakdown."""
        try:
            resp = await self._client.get("/costs", params={"hours": hours})
            resp.raise_for_status()
            return dict(resp.json())
        except Exception:
            return {}

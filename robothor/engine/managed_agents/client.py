"""Async HTTP client for the Claude Managed Agents REST API.

Wraps agent, environment, session, and memory-store endpoints behind
a thin async interface.  SSE streaming is exposed as an async iterator.

The singleton ``get_ma_client()`` lazily creates a client using the
``ANTHROPIC_API_KEY`` environment variable.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from robothor.engine.managed_agents.models import (
        MAAgentConfig,
        MAEnvironmentConfig,
        MASessionConfig,
    )

logger = logging.getLogger(__name__)

_BETA_HEADER = "managed-agents-2026-04-01"
_API_VERSION = "2023-06-01"
_BASE_URL = "https://api.anthropic.com"


class MAClientError(Exception):
    """Non-retryable error from the Managed Agents API."""


class MAUnavailableError(Exception):
    """Retryable error — API is down or rate-limited."""


class ManagedAgentsClient:
    """Async client for the Claude Managed Agents API (beta)."""

    def __init__(self, api_key: str, *, base_url: str = _BASE_URL) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _API_VERSION,
                "anthropic-beta": _BETA_HEADER,
                "content-type": "application/json",
            },
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
        )

    # ── Agent CRUD ─────────────────────────────────────────────────────

    async def create_agent(self, config: MAAgentConfig) -> dict[str, Any]:
        """Create a new agent definition.  Returns ``{"id": ..., "version": ...}``."""
        body: dict[str, Any] = {
            "name": config.name,
            "model": config.model,
            "tools": config.tools,
        }
        if config.system_prompt:
            body["system"] = config.system_prompt
        if config.callable_agents:
            body["callable_agents"] = config.callable_agents
        return await self._post("/v1/agents", body)

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._get(f"/v1/agents/{agent_id}")

    async def update_agent(self, agent_id: str, config: MAAgentConfig) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": config.name,
            "model": config.model,
            "tools": config.tools,
        }
        if config.system_prompt:
            body["system"] = config.system_prompt
        if config.callable_agents:
            body["callable_agents"] = config.callable_agents
        return await self._post(f"/v1/agents/{agent_id}", body)

    async def list_agents(self) -> list[dict[str, Any]]:
        resp = await self._get("/v1/agents")
        return resp.get("data", [])

    # ── Environment CRUD ──────────────────────────────────────────────

    async def create_environment(self, config: MAEnvironmentConfig) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": config.name,
            "config": {
                "type": "cloud",
                "networking": {"type": config.networking},
            },
        }
        return await self._post("/v1/environments", body)

    async def get_environment(self, env_id: str) -> dict[str, Any]:
        return await self._get(f"/v1/environments/{env_id}")

    # ── Session lifecycle ─────────────────────────────────────────────

    async def create_session(self, config: MASessionConfig) -> dict[str, Any]:
        body: dict[str, Any] = {
            "agent": config.agent_id,
        }
        if config.environment_id:
            body["environment_id"] = config.environment_id
        if config.resources:
            body["resources"] = config.resources
        if config.title:
            body["title"] = config.title
        return await self._post("/v1/sessions", body)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._get(f"/v1/sessions/{session_id}")

    async def send_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        """Send events (user messages, tool results, etc.) to a session."""
        await self._post(f"/v1/sessions/{session_id}/events", {"events": events})

    async def stream_session(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Open an SSE stream for a session.  Yields parsed event dicts.

        Each dict has at minimum a ``"type"`` key.  The caller should
        break on ``session.status_idle`` or ``session.status_terminated``.
        """
        url = f"/v1/sessions/{session_id}/stream"
        try:
            async with self._client.stream(
                "GET",
                url,
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
            ) as response:
                if response.status_code >= 500:
                    raise MAUnavailableError(f"MA stream returned {response.status_code}")
                if response.status_code >= 400:
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                    raise MAClientError(f"MA stream error {response.status_code}: {body.decode()}")

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]  # strip "data: " prefix
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Malformed SSE data: %s", raw[:200])
        except httpx.ConnectError as exc:
            raise MAUnavailableError(f"Cannot connect to MA API: {exc}") from exc
        except httpx.ReadTimeout as exc:
            raise MAUnavailableError(f"MA stream read timeout: {exc}") from exc

    async def archive_session(self, session_id: str) -> dict[str, Any]:
        return await self._post(f"/v1/sessions/{session_id}/archive", {})

    # ── Memory Store CRUD ─────────────────────────────────────────────

    async def create_memory_store(self, name: str, description: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        return await self._post("/v1/memory_stores", body)

    async def get_memory_store(self, store_id: str) -> dict[str, Any]:
        return await self._get(f"/v1/memory_stores/{store_id}")

    async def write_memory(self, store_id: str, path: str, content: str) -> dict[str, Any]:
        return await self._post(
            f"/v1/memory_stores/{store_id}/memories",
            {"path": path, "content": content},
        )

    async def search_memories(self, store_id: str, query: str) -> list[dict[str, Any]]:
        resp = await self._get(
            f"/v1/memory_stores/{store_id}/memories",
            params={"path_prefix": "/"},
        )
        return resp.get("data", [])

    async def list_memories(self, store_id: str) -> list[dict[str, Any]]:
        resp = await self._get(
            f"/v1/memory_stores/{store_id}/memories",
            params={"path_prefix": "/"},
        )
        return resp.get("data", [])

    # ── Internals ─────────────────────────────────────────────────────

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(path, json=body)
        except httpx.ConnectError as exc:
            raise MAUnavailableError(f"Cannot connect to MA API: {exc}") from exc
        except httpx.ReadTimeout as exc:
            raise MAUnavailableError(f"MA API read timeout: {exc}") from exc

        if resp.status_code >= 500:
            raise MAUnavailableError(f"MA API {resp.status_code}: {resp.text[:500]}")
        if resp.status_code == 429:
            raise MAUnavailableError(f"MA API rate limited: {resp.text[:500]}")
        if resp.status_code >= 400:
            raise MAClientError(f"MA API {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.ConnectError as exc:
            raise MAUnavailableError(f"Cannot connect to MA API: {exc}") from exc
        except httpx.ReadTimeout as exc:
            raise MAUnavailableError(f"MA API read timeout: {exc}") from exc

        if resp.status_code >= 500:
            raise MAUnavailableError(f"MA API {resp.status_code}: {resp.text[:500]}")
        if resp.status_code == 429:
            raise MAUnavailableError(f"MA API rate limited: {resp.text[:500]}")
        if resp.status_code >= 400:
            raise MAClientError(f"MA API {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()


# ── Singleton ─────────────────────────────────────────────────────────

_client_instance: ManagedAgentsClient | None = None


def get_ma_client() -> ManagedAgentsClient:
    """Return the singleton ManagedAgentsClient (lazily created).

    Reads ``ANTHROPIC_API_KEY`` from the environment on first call.
    """
    global _client_instance
    if _client_instance is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY environment variable is required for Managed Agents"
            )
        _client_instance = ManagedAgentsClient(api_key)
    return _client_instance


def reset_ma_client() -> None:
    """Reset the singleton — used in tests."""
    global _client_instance
    _client_instance = None

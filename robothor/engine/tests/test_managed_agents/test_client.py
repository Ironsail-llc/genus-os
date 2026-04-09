"""Tests for managed_agents.client — HTTP client with mocked responses."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from robothor.engine.managed_agents.client import (
    MAClientError,
    ManagedAgentsClient,
    MAUnavailableError,
    get_ma_client,
    reset_ma_client,
)
from robothor.engine.managed_agents.models import (
    MAAgentConfig,
    MAEnvironmentConfig,
    MASessionConfig,
)


@pytest.fixture()
def client():
    """Create a client with a fake API key."""
    return ManagedAgentsClient("test-api-key", base_url="https://test.api.anthropic.com")


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure singleton is clean between tests."""
    reset_ma_client()
    yield
    reset_ma_client()


# ── Agent CRUD ────────────────────────────────────────────────────────


class TestCreateAgent:
    @pytest.mark.asyncio
    async def test_success(self, client):
        resp_body = {"id": "agent_123", "version": 1}
        mock_resp = httpx.Response(200, json=resp_body)
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        result = await client.create_agent(
            MAAgentConfig(name="test", model="claude-sonnet-4-6", tools=[])
        )
        assert result["id"] == "agent_123"
        assert result["version"] == 1

    @pytest.mark.asyncio
    async def test_server_error_raises_unavailable(self, client):
        mock_resp = httpx.Response(500, text="Internal Server Error")
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(MAUnavailableError, match="500"):
            await client.create_agent(MAAgentConfig(name="t", model="m", tools=[]))

    @pytest.mark.asyncio
    async def test_client_error_raises_client_error(self, client):
        mock_resp = httpx.Response(400, text="Bad Request")
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(MAClientError, match="400"):
            await client.create_agent(MAAgentConfig(name="t", model="m", tools=[]))

    @pytest.mark.asyncio
    async def test_rate_limit_raises_unavailable(self, client):
        mock_resp = httpx.Response(429, text="Rate limited")
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(MAUnavailableError, match="rate limited"):
            await client.create_agent(MAAgentConfig(name="t", model="m", tools=[]))

    @pytest.mark.asyncio
    async def test_connect_error_raises_unavailable(self, client):
        client._client = AsyncMock()
        client._client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(MAUnavailableError, match="Cannot connect"):
            await client.create_agent(MAAgentConfig(name="t", model="m", tools=[]))


# ── Environment CRUD ──────────────────────────────────────────────────


class TestCreateEnvironment:
    @pytest.mark.asyncio
    async def test_success(self, client):
        resp_body = {"id": "env_456"}
        mock_resp = httpx.Response(200, json=resp_body)
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        result = await client.create_environment(MAEnvironmentConfig(name="test-env"))
        assert result["id"] == "env_456"


# ── Session lifecycle ─────────────────────────────────────────────────


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_success(self, client):
        resp_body = {"id": "session_789", "status": "rescheduling"}
        mock_resp = httpx.Response(200, json=resp_body)
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        result = await client.create_session(
            MASessionConfig(agent_id="agent_123", environment_id="env_456")
        )
        assert result["id"] == "session_789"


class TestSendEvents:
    @pytest.mark.asyncio
    async def test_sends_events(self, client):
        mock_resp = httpx.Response(200, json={})
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        await client.send_events(
            "session_789",
            [{"type": "user.message", "content": [{"type": "text", "text": "hello"}]}],
        )
        client._client.post.assert_called_once()
        call_args = client._client.post.call_args
        assert "events" in call_args.kwargs.get("json", call_args[1].get("json", {}))


# ── Memory Store ──────────────────────────────────────────────────────


class TestMemoryStore:
    @pytest.mark.asyncio
    async def test_create_memory_store(self, client):
        resp_body = {"id": "memstore_abc"}
        mock_resp = httpx.Response(200, json=resp_body)
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        result = await client.create_memory_store("test-store", "A test store")
        assert result["id"] == "memstore_abc"

    @pytest.mark.asyncio
    async def test_write_memory(self, client):
        resp_body = {"id": "mem_123", "path": "/test.md"}
        mock_resp = httpx.Response(200, json=resp_body)
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=mock_resp)

        result = await client.write_memory("memstore_abc", "/test.md", "Hello world")
        assert result["path"] == "/test.md"


# ── Singleton ─────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_ma_client_requires_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove ANTHROPIC_API_KEY if set
            import os

            os.environ.pop("ANTHROPIC_API_KEY", None)
            reset_ma_client()
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                get_ma_client()

    def test_get_ma_client_creates_singleton(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            reset_ma_client()
            c1 = get_ma_client()
            c2 = get_ma_client()
            assert c1 is c2

    def test_reset_clears_singleton(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            reset_ma_client()
            c1 = get_ma_client()
            reset_ma_client()
            c2 = get_ma_client()
            assert c1 is not c2

"""Tests for managed_agents.tenant_mapper — resource caching."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.managed_agents.tenant_mapper import (
    TenantMapper,
    reset_tenant_mapper,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_tenant_mapper()
    yield
    reset_tenant_mapper()


@pytest.fixture()
def mock_client():
    client = AsyncMock()
    client.create_agent = AsyncMock(return_value={"id": "agent_ma_1", "version": 1})
    client.create_environment = AsyncMock(return_value={"id": "env_ma_1"})
    client.create_memory_store = AsyncMock(return_value={"id": "memstore_ma_1"})
    return client


class TestTenantMapperAgent:
    @pytest.mark.asyncio
    async def test_creates_agent_on_first_call(self, mock_client):
        mapper = TenantMapper(mock_client)

        # Mock DB lookup returning None (no cached entry)
        with (
            patch(
                "robothor.engine.managed_agents.tenant_mapper._lookup_resource",
                return_value=None,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper._cache_resource",
            ) as mock_cache,
        ):
            result = await mapper.get_or_create_agent(
                "test-tenant", "main", "claude-sonnet-4-6", "You are helpful", []
            )

        assert result["id"] == "agent_ma_1"
        assert result["version"] == 1
        mock_client.create_agent.assert_called_once()
        mock_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_cached_agent(self, mock_client):
        mapper = TenantMapper(mock_client)

        with patch(
            "robothor.engine.managed_agents.tenant_mapper._lookup_resource",
            return_value={"ma_resource_id": "agent_cached", "ma_version": 2},
        ):
            result = await mapper.get_or_create_agent("test-tenant", "main", "m", "s", [])

        assert result["id"] == "agent_cached"
        assert result["version"] == 2
        mock_client.create_agent.assert_not_called()


class TestTenantMapperEnvironment:
    @pytest.mark.asyncio
    async def test_creates_environment(self, mock_client):
        mapper = TenantMapper(mock_client)

        with (
            patch(
                "robothor.engine.managed_agents.tenant_mapper._lookup_resource",
                return_value=None,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper._cache_resource",
            ),
        ):
            result = await mapper.get_or_create_environment("test-tenant", "default")

        assert result == "env_ma_1"
        mock_client.create_environment.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_cached_environment(self, mock_client):
        mapper = TenantMapper(mock_client)

        with patch(
            "robothor.engine.managed_agents.tenant_mapper._lookup_resource",
            return_value={"ma_resource_id": "env_cached"},
        ):
            result = await mapper.get_or_create_environment("test-tenant")

        assert result == "env_cached"
        mock_client.create_environment.assert_not_called()


class TestTenantMapperMemoryStore:
    @pytest.mark.asyncio
    async def test_creates_memory_store(self, mock_client):
        mapper = TenantMapper(mock_client)

        with (
            patch(
                "robothor.engine.managed_agents.tenant_mapper._lookup_resource",
                return_value=None,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper._cache_resource",
            ),
        ):
            result = await mapper.get_or_create_memory_store("test-tenant", "agent-memory")

        assert result == "memstore_ma_1"
        mock_client.create_memory_store.assert_called_once()


class TestTenantMapperInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_calls_delete(self, mock_client):
        mapper = TenantMapper(mock_client)

        with patch(
            "robothor.engine.managed_agents.tenant_mapper._delete_resource",
        ) as mock_delete:
            await mapper.invalidate("agent", "test-tenant", "main")

        mock_delete.assert_called_once_with("agent", "test-tenant", "main")

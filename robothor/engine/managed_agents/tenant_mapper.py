"""Map tenant IDs to Managed Agents resource IDs.

Each tenant gets isolated MA resources (agents, environments, memory stores).
The mapping is cached in the ``ma_tenant_resources`` database table so that
resources are created once and reused across sessions.

Does NOT modify any existing tables or tenant infrastructure.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.managed_agents.client import ManagedAgentsClient

from robothor.engine.managed_agents.models import (
    MAAgentConfig,
    MAEnvironmentConfig,
)

logger = logging.getLogger(__name__)


class TenantMapper:
    """Lazily create and cache Managed Agents resources per tenant."""

    def __init__(self, client: ManagedAgentsClient) -> None:
        self._client = client

    # ── Public interface ──────────────────────────────────────────────

    async def get_or_create_agent(
        self,
        tenant_id: str,
        agent_id: str,
        model: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return ``{"id": "<ma_agent_id>", "version": <int>}``.

        Creates the MA agent on first call for this tenant+agent pair,
        then caches in the database.
        """
        cached = _lookup_resource("agent", tenant_id, agent_id)
        if cached:
            return {"id": str(cached["ma_resource_id"]), "version": cached.get("ma_version", 1)}

        config = MAAgentConfig(
            name=f"{tenant_id}/{agent_id}",
            model=model,
            system_prompt=system_prompt,
            tools=tools,
        )
        ma_agent = await self._client.create_agent(config)
        _cache_resource(
            "agent",
            tenant_id,
            agent_id,
            ma_agent["id"],
            version=ma_agent.get("version", 1),
            config_snapshot={"model": model, "tools_count": len(tools)},
        )
        logger.info(
            "Created MA agent %s for tenant=%s agent=%s",
            ma_agent["id"],
            tenant_id,
            agent_id,
        )
        return {"id": str(ma_agent["id"]), "version": ma_agent.get("version", 1)}

    async def get_or_create_environment(
        self,
        tenant_id: str,
        env_name: str = "default",
    ) -> str:
        """Return the MA environment ID, creating if needed."""
        cached = _lookup_resource("environment", tenant_id, env_name)
        if cached:
            return str(cached["ma_resource_id"])

        config = MAEnvironmentConfig(name=f"{tenant_id}/{env_name}")
        ma_env = await self._client.create_environment(config)
        _cache_resource("environment", tenant_id, env_name, ma_env["id"])
        logger.info(
            "Created MA environment %s for tenant=%s",
            ma_env["id"],
            tenant_id,
        )
        return str(ma_env["id"])

    async def get_or_create_memory_store(
        self,
        tenant_id: str,
        store_name: str,
    ) -> str:
        """Return the MA memory-store ID, creating if needed."""
        cached = _lookup_resource("memory_store", tenant_id, store_name)
        if cached:
            return str(cached["ma_resource_id"])

        store = await self._client.create_memory_store(
            name=f"{tenant_id}/{store_name}",
            description=f"Memory store for tenant {tenant_id}",
        )
        _cache_resource("memory_store", tenant_id, store_name, store["id"])
        logger.info(
            "Created MA memory store %s for tenant=%s store=%s",
            store["id"],
            tenant_id,
            store_name,
        )
        return str(store["id"])

    async def invalidate(self, resource_type: str, tenant_id: str, resource_name: str) -> None:
        """Delete a cached mapping so the next call re-creates the resource."""
        _delete_resource(resource_type, tenant_id, resource_name)


# ── Database helpers (sync, using psycopg2 pool) ─────────────────────


def _lookup_resource(
    resource_type: str, tenant_id: str, resource_name: str
) -> dict[str, Any] | None:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ma_resource_id, ma_version, ma_config
                    FROM ma_tenant_resources
                    WHERE resource_type = %s
                      AND tenant_id = %s
                      AND resource_name = %s
                    """,
                    (resource_type, tenant_id, resource_name),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "ma_resource_id": row[0],
                        "ma_version": row[1],
                        "ma_config": row[2] or {},
                    }
    except Exception:
        logger.debug(
            "ma_tenant_resources lookup failed (table may not exist yet)",
            exc_info=True,
        )
    return None


def _cache_resource(
    resource_type: str,
    tenant_id: str,
    resource_name: str,
    ma_resource_id: str,
    *,
    version: int = 1,
    config_snapshot: dict[str, Any] | None = None,
) -> None:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ma_tenant_resources (
                        tenant_id, resource_type, resource_name,
                        ma_resource_id, ma_version, ma_config
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, resource_type, resource_name)
                    DO UPDATE SET
                        ma_resource_id = EXCLUDED.ma_resource_id,
                        ma_version = EXCLUDED.ma_version,
                        ma_config = EXCLUDED.ma_config,
                        updated_at = NOW()
                    """,
                    (
                        tenant_id,
                        resource_type,
                        resource_name,
                        ma_resource_id,
                        version,
                        json.dumps(config_snapshot or {}),
                    ),
                )
                conn.commit()
    except Exception:
        logger.warning("Failed to cache MA resource mapping (non-fatal)", exc_info=True)


def _delete_resource(resource_type: str, tenant_id: str, resource_name: str) -> None:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM ma_tenant_resources
                    WHERE resource_type = %s
                      AND tenant_id = %s
                      AND resource_name = %s
                    """,
                    (resource_type, tenant_id, resource_name),
                )
                conn.commit()
    except Exception:
        logger.warning("Failed to delete MA resource mapping", exc_info=True)


# ── Singleton ─────────────────────────────────────────────────────────

_mapper: TenantMapper | None = None


def get_tenant_mapper() -> TenantMapper:
    """Return the singleton TenantMapper (lazily created)."""
    global _mapper
    if _mapper is None:
        from robothor.engine.managed_agents.client import get_ma_client

        _mapper = TenantMapper(get_ma_client())
    return _mapper


def reset_tenant_mapper() -> None:
    """Reset the singleton — used in tests."""
    global _mapper
    _mapper = None

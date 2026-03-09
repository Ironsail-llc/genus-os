"""Cross-instance command dispatch — trigger agents, query health, push config."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.federation.models import ConnectionState, SyncChannel

if TYPE_CHECKING:
    from robothor.federation.connections import ConnectionManager
    from robothor.federation.nats import NATSManager

logger = logging.getLogger(__name__)


class CommandDispatcher:
    """Dispatches commands to connected peer instances.

    Commands are request/response over NATS. The calling instance sends
    a command and waits for the response (with timeout).
    """

    def __init__(
        self,
        nats_manager: NATSManager,
        connection_manager: ConnectionManager,
    ) -> None:
        self._nats = nats_manager
        self._connections = connection_manager

    async def query_health(self, connection_id: str) -> dict[str, Any]:
        """Query a connected instance's health status."""
        conn = self._connections.get(connection_id)
        if not conn or conn.state != ConnectionState.ACTIVE:
            return {"error": "Connection not active"}

        if not self._connections.is_imported(connection_id, "health"):
            return {"error": "Health data not imported from this peer"}

        return await self._send_command(
            connection_id,
            command="query_health",
            payload={},
        )

    async def query_agent_runs(
        self,
        connection_id: str,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Query a connected instance's recent agent runs."""
        conn = self._connections.get(connection_id)
        if not conn or conn.state != ConnectionState.ACTIVE:
            return {"error": "Connection not active"}

        if not self._connections.is_imported(connection_id, "agent_runs"):
            return {"error": "Agent runs not imported from this peer"}

        return await self._send_command(
            connection_id,
            command="query_agent_runs",
            payload={"agent_id": agent_id, "limit": limit},
        )

    async def trigger_agent(
        self,
        connection_id: str,
        agent_id: str,
        message: str = "",
    ) -> dict[str, Any]:
        """Trigger an agent run on a connected instance."""
        conn = self._connections.get(connection_id)
        if not conn or conn.state != ConnectionState.ACTIVE:
            return {"error": "Connection not active"}

        return await self._send_command(
            connection_id,
            command="trigger_agent",
            payload={"agent_id": agent_id, "message": message},
        )

    async def search_memory(
        self,
        connection_id: str,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search a connected instance's memory."""
        conn = self._connections.get(connection_id)
        if not conn or conn.state != ConnectionState.ACTIVE:
            return {"error": "Connection not active"}

        if not self._connections.is_imported(connection_id, "memory_search"):
            return {"error": "Memory search not imported from this peer"}

        return await self._send_command(
            connection_id,
            command="search_memory",
            payload={"query": query, "limit": limit},
        )

    async def push_config(
        self,
        connection_id: str,
        config_key: str,
        config_value: Any,
    ) -> dict[str, Any]:
        """Push configuration to a connected instance."""
        conn = self._connections.get(connection_id)
        if not conn or conn.state != ConnectionState.ACTIVE:
            return {"error": "Connection not active"}

        if not self._connections.is_exported(connection_id, "config_push"):
            return {"error": "Config push not exported to this peer"}

        return await self._send_command(
            connection_id,
            command="push_config",
            payload={"key": config_key, "value": config_value},
        )

    async def get_sync_status(self, connection_id: str) -> dict[str, Any]:
        """Get sync watermarks for all channels of a connection."""
        conn = self._connections.get(connection_id)
        if not conn:
            return {"error": "Connection not found"}

        from robothor.federation.sync import EventJournal

        journal = EventJournal(instance_id="")
        watermarks = {}
        for channel in SyncChannel:
            watermarks[channel.value] = journal.get_sync_watermark(connection_id, channel)

        unsynced = {}
        for channel in SyncChannel:
            events = journal.get_unsynced(connection_id, channel, limit=0)
            unsynced[channel.value] = len(events)

        return {
            "connection_id": connection_id,
            "peer_name": conn.peer_name,
            "state": conn.state.value,
            "watermarks": watermarks,
            "unsynced_counts": unsynced,
        }

    async def _send_command(
        self,
        connection_id: str,
        command: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a command via NATS and return the response."""
        message = {
            "command": command,
            "payload": payload,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        data = json.dumps(message).encode()

        success = await self._nats.publish_command(connection_id, data)
        if not success:
            return {"error": f"Failed to send command: {command}"}

        # For now, commands are fire-and-forget. Request-reply will be
        # added when NATS infrastructure is deployed.
        return {"sent": True, "command": command}

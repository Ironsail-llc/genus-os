"""Federation tool handlers — agent-usable tools for cross-instance operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("federation_query")
async def _federation_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Query a connected instance's data (health, runs, memory)."""
    connection_id = args.get("connection_id", "")
    query_type = args.get("query_type", "health")

    if not connection_id:
        return {"error": "connection_id is required"}

    def _run() -> dict[str, Any]:
        try:
            from robothor.federation.connections import ConnectionManager, load_connections
            from robothor.federation.models import ConnectionState

            mgr = ConnectionManager()
            for conn in load_connections():
                mgr.add(conn)

            connection = mgr.get(connection_id)
            if not connection:
                return {"error": f"Connection not found: {connection_id}"}
            if connection.state != ConnectionState.ACTIVE:
                return {"error": f"Connection not active (state={connection.state.value})"}

            if query_type == "health":
                return {
                    "connection_id": connection_id,
                    "peer_name": connection.peer_name,
                    "state": connection.state.value,
                    "relationship": connection.relationship.value,
                    "exports": connection.exports,
                    "imports": connection.imports,
                }
            if query_type == "runs":
                agent_id = args.get("agent_id")
                limit = args.get("limit", 20)
                return {
                    "note": "Remote agent run queries require NATS transport (not yet deployed)",
                    "connection_id": connection_id,
                    "peer_name": connection.peer_name,
                    "agent_id": agent_id,
                    "limit": limit,
                }
            return {"error": f"Unknown query type: {query_type}"}
        except Exception as e:
            return {"error": f"Federation query failed: {e}"}

    return await asyncio.to_thread(_run)


@_handler("federation_trigger")
async def _federation_trigger(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Trigger an agent run on a connected instance."""
    connection_id = args.get("connection_id", "")
    agent_id = args.get("agent_id", "")
    message = args.get("message", "")

    if not connection_id or not agent_id:
        return {"error": "connection_id and agent_id are required"}

    def _run() -> dict[str, Any]:
        try:
            from robothor.federation.connections import ConnectionManager, load_connections
            from robothor.federation.models import ConnectionState

            mgr = ConnectionManager()
            for conn in load_connections():
                mgr.add(conn)

            connection = mgr.get(connection_id)
            if not connection:
                return {"error": f"Connection not found: {connection_id}"}
            if connection.state != ConnectionState.ACTIVE:
                return {"error": f"Connection not active (state={connection.state.value})"}

            return {
                "note": "Remote agent triggers require NATS transport (not yet deployed)",
                "connection_id": connection_id,
                "peer_name": connection.peer_name,
                "agent_id": agent_id,
                "message": message[:200],
            }
        except Exception as e:
            return {"error": f"Federation trigger failed: {e}"}

    return await asyncio.to_thread(_run)


@_handler("federation_sync_status")
async def _federation_sync_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Check sync watermarks and unsynced event counts for a connection."""
    connection_id = args.get("connection_id")

    def _run() -> dict[str, Any]:
        try:
            from robothor.federation.connections import ConnectionManager, load_connections
            from robothor.federation.models import SyncChannel
            from robothor.federation.sync import EventJournal

            mgr = ConnectionManager()
            for conn in load_connections():
                mgr.add(conn)

            if connection_id:
                connections = [mgr.get(connection_id)]
                if connections[0] is None:
                    return {"error": f"Connection not found: {connection_id}"}
            else:
                connections = mgr.list_all()

            results = []
            journal = EventJournal(instance_id="")
            for conn in connections:
                if conn is None:
                    continue
                watermarks = {}
                unsynced = {}
                for channel in SyncChannel:
                    watermarks[channel.value] = journal.get_sync_watermark(conn.id, channel)
                    events = journal.get_unsynced(conn.id, channel, limit=1000)
                    unsynced[channel.value] = len(events)

                results.append(
                    {
                        "connection_id": conn.id,
                        "peer_name": conn.peer_name,
                        "state": conn.state.value,
                        "watermarks": watermarks,
                        "unsynced_counts": unsynced,
                    }
                )

            return {"connections": results}
        except Exception as e:
            return {"error": f"Sync status check failed: {e}"}

    return await asyncio.to_thread(_run)

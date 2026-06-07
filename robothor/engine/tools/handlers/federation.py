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
                return {"_nats": True, "peer_name": connection.peer_name}
            return {"error": f"Unknown query type: {query_type}"}
        except Exception as e:
            return {"error": f"Federation query failed: {e}"}

    result = await asyncio.to_thread(_run)
    if not result.get("_nats"):
        return result
    # Remote run query over the NATS request-reply transport.
    return await _federation_request(
        connection_id,
        {"op": "list_runs", "agent_id": args.get("agent_id"), "limit": args.get("limit", 20)},
    )


async def _federation_request(connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a NATS request to a peer and return its JSON reply (or an honest error)."""
    import json

    from robothor.federation.nats import get_nats_manager

    nats_mgr = get_nats_manager()
    if nats_mgr is None or not nats_mgr.is_connected:
        return {
            "error": "Federation transport not connected — enable NATS "
            "(helm nats.enabled=true / ROBOTHOR_NATS_URL) and pair the instances.",
            "connection_id": connection_id,
        }
    reply = await nats_mgr.request(connection_id, json.dumps(payload).encode(), timeout=5.0)
    if reply is None:
        return {
            "error": "Federation request timed out or peer unavailable",
            "connection_id": connection_id,
        }
    try:
        return dict(json.loads(reply))
    except Exception:
        return {"raw": reply.decode(errors="ignore"), "connection_id": connection_id}


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

            return {"_nats": True, "peer_name": connection.peer_name}
        except Exception as e:
            return {"error": f"Federation trigger failed: {e}"}

    result = await asyncio.to_thread(_run)
    if not result.get("_nats"):
        return result
    return await _federation_request(
        connection_id, {"op": "trigger", "agent_id": agent_id, "message": message}
    )


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
                found = mgr.get(connection_id)
                if found is None:
                    return {"error": f"Connection not found: {connection_id}"}
                connections = [found]
            else:
                connections = mgr.list_all()

            results = []
            journal = EventJournal(instance_id="")
            for conn in connections:
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

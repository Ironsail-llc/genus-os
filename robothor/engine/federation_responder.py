"""Inbound federation responder.

Answers a peer's ``federation_query``/``federation_trigger`` over the NATS
request-reply transport. The client side (``tools/handlers/federation.py``)
could already SEND these ops, but nothing on the daemon side ANSWERED them —
so a round-trip always failed. This wires ``NATSManager.serve_requests`` to a
per-connection handler that authorizes each op against the capabilities WE
export to that peer, then executes it locally.

Lives in the engine layer (not ``robothor/federation``) because serving a
``trigger`` needs the engine runner — the federation package must not import
the engine.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from robothor.engine.tools.handlers.federation import _OP_REQUIRED_CAPABILITY

logger = logging.getLogger(__name__)


def make_command_handler(connection: Any, runner: Any) -> Any:
    """Build an async ``(bytes) -> bytes`` handler serving one connection.

    ``connection.exports`` is the set of capabilities we expose to this peer; an
    op is refused unless its required capability is exported (the inbound mirror
    of the outbound ``_authorize_op`` gate).
    """

    exports = set(getattr(connection, "exports", []) or [])

    async def handle(data: bytes) -> bytes:
        try:
            payload = json.loads(data)
        except Exception:
            return b'{"error": "malformed request"}'

        op = payload.get("op")
        required = _OP_REQUIRED_CAPABILITY.get(op)
        if required is None:
            return json.dumps({"error": f"unknown op: {op}"}).encode()
        if required not in exports:
            return json.dumps(
                {"error": f"op '{op}' not authorized — capability '{required}' not exported"}
            ).encode()

        try:
            if op == "list_runs":
                return _list_runs(payload)
            if op == "trigger":
                return await _trigger(payload, runner)
        except Exception as e:  # noqa: BLE001
            logger.warning("Federation responder op %s failed: %s", op, e)
            return json.dumps({"error": "op execution failed"}).encode()
        return json.dumps({"error": f"unhandled op: {op}"}).encode()

    return handle


def _list_runs(payload: dict[str, Any]) -> bytes:
    from robothor.db.connection import get_connection

    agent_id = payload.get("agent_id")
    limit = min(int(payload.get("limit", 20)), 100)
    with get_connection() as conn:
        cur = conn.cursor()
        if agent_id:
            cur.execute(
                "SELECT id::text, agent_id, status, started_at "
                "FROM agent_runs WHERE agent_id = %s ORDER BY started_at DESC LIMIT %s",
                (agent_id, limit),
            )
        else:
            cur.execute(
                "SELECT id::text, agent_id, status, started_at "
                "FROM agent_runs ORDER BY started_at DESC LIMIT %s",
                (limit,),
            )
        runs = [
            {"run_id": r[0], "agent_id": r[1], "status": r[2], "started_at": str(r[3])}
            for r in cur.fetchall()
        ]
    return json.dumps({"runs": runs, "count": len(runs)}).encode()


async def _trigger(payload: dict[str, Any], runner: Any) -> bytes:
    from robothor.engine.models import TriggerType

    agent_id = payload.get("agent_id")
    if not agent_id:
        return json.dumps({"error": "agent_id required"}).encode()
    run = await runner.execute(
        agent_id=agent_id,
        message=payload.get("message", ""),
        trigger_type=TriggerType.FEDERATION,
        trigger_detail="federation:peer",
    )
    return json.dumps({"triggered": True, "run_id": getattr(run, "id", None)}).encode()

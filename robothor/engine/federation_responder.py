"""Inbound federation responder — the security boundary of this feature.

A peer's ``federation_query``/``federation_trigger`` arrives here over NATS
request-reply and is executed locally. That makes this file the place where
another instance's authority over ours is decided, so it is worth stating what
it used to do:

* ``_trigger`` called ``runner.execute()`` with **no user_id, no user_role and
  no tenant_id**. ``TriggerType.FEDERATION`` is a system trigger, so
  ``runner.py:601`` fell back to ``agent_config.service_role or "service"`` --
  and migration 107 seeds ``service -> ('*','allow')``. A peer holding a single
  capability therefore got allow-all tool access, recorded against no principal.
* ``_list_runs`` was raw SQL with **no tenant predicate**, returning every
  tenant's runs.
* Both ops required the same capability, ``agent_runs``, which was a CHILD
  default -- so a child could execute arbitrary agents on its parent by default.

Now every inbound op passes three independent gates, each of which refuses on
its own:

1. **Capability** -- ``_authorize_op`` against what WE export to this peer.
2. **Authorization** -- ``check_tool_permission`` under the connection's
   principal role (``federation_child`` is deny-all; ``federation_parent`` is
   read-only), with per-connection tightening available through
   ``user_permissions`` keyed on ``federation:<connection_id>``.
3. **Tenancy** -- every op runs inside ``tenant_scope``, so a query that forgets
   its WHERE clause still cannot cross tenants.

The transport adds a fourth outside this file: per-connection NATS accounts,
where a parent exports no service to a child, so a child has no subject on
which to address its parent at all.

Lives in the engine layer (not ``robothor/federation``) because serving a
``trigger`` needs the engine runner, and the federation package must not import
the engine.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from robothor.engine.tools.handlers.federation import _OP_REQUIRED_CAPABILITY

logger = logging.getLogger(__name__)

#: Tool name each op is checked against in the local authorization layer. These
#: are the LOCAL permission names, distinct from the wire capability: the
#: capability says "this peer was granted the right to ask", the tool name says
#: "this principal is permitted to do it here". Both must pass.
_OP_LOCAL_TOOL = {
    "list_runs": "list_agent_runs",
    "trigger": "exec",
    "health": "get_stats",
}


#: The one op served before activation. Kept as a literal rather than an
#: import so the responder does not pull the crypto stack in at import time.
HANDSHAKE_OP = "handshake"


def _principal(connection: Any) -> tuple[str, str, str]:
    """(principal_id, principal_role, tenant_id) for this connection.

    Falls back to a deny-all role rather than to nothing: an unseeded or absent
    role makes ``check_tool_permission`` fail closed, which is the correct
    direction for a connection whose principal was never established.
    """
    cid = getattr(connection, "id", "") or "unknown"
    pid = getattr(connection, "local_principal_id", None) or f"federation:{cid}"
    role = getattr(connection, "local_principal_role", None) or "federation_child"
    tenant = getattr(connection, "tenant_id", None) or "default"
    return pid, role, tenant


def make_command_handler(
    connection: Any,
    runner: Any,
    *,
    config: Any = None,
    on_activate: Any = None,
) -> Any:
    """Build an async ``(bytes) -> bytes`` handler serving one connection.

    ``config`` is the FederationConfig used to sign the handshake ack; without
    it this connection can serve traffic but cannot complete a pairing.
    ``on_activate`` is called with the connection once a handshake succeeds, so
    the caller can persist the state transition it just earned.
    """

    exports = set(getattr(connection, "exports", []) or [])
    principal_id, principal_role, tenant_id = _principal(connection)

    async def handle(data: bytes) -> bytes:
        try:
            payload = json.loads(data)
        except Exception:
            return b'{"error": "malformed request"}'

        op = payload.get("op")

        # Gate 0 — is this connection allowed to carry traffic at all?
        #
        # Read the state on every call rather than capturing it: a successful
        # handshake mutates the connection in place, and suspending a child has
        # to stop traffic that is already flowing, not merely change a string
        # that some earlier closure captured.
        state = getattr(connection, "state", None)
        state_value = getattr(state, "value", state)
        if op == HANDSHAKE_OP:
            if state_value == "active":
                _audit(op, principal_id, tenant_id, "denied", "already active")
                return _err(
                    "connection is already active — re-pairing an established "
                    "link goes through the operator, not the invite"
                )
            if state_value != "pending":
                _audit(op, principal_id, tenant_id, "denied", f"state {state_value}")
                return _err(f"connection is not active (state: {state_value})")
            return await _handshake(
                payload_bytes=data,
                connection=connection,
                config=config,
                on_activate=on_activate,
                principal_id=principal_id,
                tenant_id=tenant_id,
            )
        if state_value != "active":
            _audit(op, principal_id, tenant_id, "denied", f"state {state_value}")
            return _err(f"connection is not active (state: {state_value})")

        required = _OP_REQUIRED_CAPABILITY.get(op)
        if required is None:
            return _err(f"unknown op: {op}")

        # Gate 1 — did we grant this peer the right to ask?
        if required not in exports:
            _audit(op, principal_id, tenant_id, "denied", f"capability {required!r} not exported")
            return _err(f"op '{op}' not authorized — capability '{required}' not exported")

        # Gate 2 — is this principal permitted to do it here?
        local_tool = _OP_LOCAL_TOOL.get(op, op)
        try:
            from robothor.engine.permissions import check_tool_permission

            denial = check_tool_permission(
                principal_role, tenant_id, local_tool, user_id=principal_id
            )
        except Exception as exc:  # noqa: BLE001 - a broken lookup must not open the gate
            logger.warning("federation permission check failed for %s: %s", op, exc)
            denial = "permission check unavailable"
        if denial:
            _audit(op, principal_id, tenant_id, "denied", denial)
            return _err(f"op '{op}' denied: {denial}")

        # Gate 3 — everything below runs scoped to this connection's tenant.
        try:
            from robothor.db.connection import tenant_scope

            if op == "list_runs":
                with tenant_scope(tenant_id):
                    out = _list_runs(payload, tenant_id)
            elif op == "health":
                out = _health()
            elif op == "trigger":
                out = await _trigger(
                    payload,
                    runner,
                    principal_id=principal_id,
                    principal_role=principal_role,
                    tenant_id=tenant_id,
                    peer_name=getattr(connection, "peer_name", ""),
                )
            else:
                return _err(f"unhandled op: {op}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Federation responder op %s failed: %s", op, e)
            _audit(op, principal_id, tenant_id, "error", type(e).__name__)
            return _err("op execution failed")

        _audit(op, principal_id, tenant_id, "ok", "")
        return out

    return handle


async def _handshake(
    *,
    payload_bytes: bytes,
    connection: Any,
    config: Any,
    on_activate: Any,
    principal_id: str,
    tenant_id: str,
) -> bytes:
    """Verify a peer's hello, activate the connection, and sign an ack.

    This is the one op that runs before authorization, because it is what
    establishes who the peer is. It grants nothing: `verify_handshake` writes
    the peer's identity and the state transition, and deliberately not the
    exports.
    """
    from robothor.federation.handshake import HandshakeError, build_ack, verify_handshake

    if config is None:
        _audit(HANDSHAKE_OP, principal_id, tenant_id, "error", "no federation config")
        return _err("handshake unavailable — this instance has no federation identity")

    try:
        verify_handshake(config, connection, payload_bytes)
    except HandshakeError as e:
        # The peer gets the reason. Five months of silence is what the
        # alternative looks like.
        _audit(HANDSHAKE_OP, principal_id, tenant_id, "denied", str(e))
        logger.warning("Federation handshake refused on %s: %s", connection.id, e)
        return _err(f"handshake refused: {e}")
    except Exception as e:  # noqa: BLE001
        _audit(HANDSHAKE_OP, principal_id, tenant_id, "error", type(e).__name__)
        logger.exception("Federation handshake failed on %s", connection.id)
        return _err("handshake failed")

    if on_activate is not None:
        try:
            on_activate(connection)
        except Exception:  # noqa: BLE001 - persistence must not undo the pairing
            logger.exception("Federation: could not persist activation of %s", connection.id)

    # Signing the ack can fail for its own reasons (no identity on disk, an
    # unreadable key). Outside the try it escaped to the NATS layer and the
    # peer received the generic "handler failed" — which says nothing about
    # what to fix, on the one code path an operator hits while pairing.
    try:
        ack = build_ack(config, connection)
    except Exception as e:  # noqa: BLE001
        _audit(HANDSHAKE_OP, principal_id, tenant_id, "error", f"ack: {type(e).__name__}")
        logger.exception("Federation: activated %s but could not sign the ack", connection.id)
        return _err(f"handshake verified but the ack could not be signed: {e}")

    _audit(HANDSHAKE_OP, principal_id, tenant_id, "ok", "activated")
    return ack


def _err(msg: str) -> bytes:
    return json.dumps({"error": msg}).encode()


def _audit(op: str, principal_id: str, tenant_id: str, outcome: str, detail: str) -> None:
    """Record every inbound op. An op that changes our state must never be
    silent, even when it was authorized."""
    try:
        from robothor.audit.logger import log_event

        log_event(
            "federation.op",
            f"federation {op} by {principal_id}: {outcome}",
            details={"op": op, "outcome": outcome, "detail": detail, "tenant_id": tenant_id},
            status="ok" if outcome == "ok" else "denied",
        )
    except Exception:  # noqa: BLE001 - audit must never break the op
        logger.debug("could not audit federation op %s", op)


def _list_runs(payload: dict[str, Any], tenant_id: str) -> bytes:
    from robothor.db.connection import get_connection

    agent_id = payload.get("agent_id")
    limit = min(int(payload.get("limit", 20)), 100)
    with get_connection() as conn:
        cur = conn.cursor()
        # The tenant predicate is explicit AND the call is wrapped in
        # tenant_scope: RLS is the backstop, not the only guard, because RLS is
        # inert when the connection is a superuser.
        if agent_id:
            cur.execute(
                "SELECT id::text, agent_id, status, started_at FROM agent_runs "
                "WHERE tenant_id = %s AND agent_id = %s "
                "ORDER BY started_at DESC LIMIT %s",
                (tenant_id, agent_id, limit),
            )
        else:
            cur.execute(
                "SELECT id::text, agent_id, status, started_at FROM agent_runs "
                "WHERE tenant_id = %s ORDER BY started_at DESC LIMIT %s",
                (tenant_id, limit),
            )
        runs = [
            {"run_id": r[0], "agent_id": r[1], "status": r[2], "started_at": str(r[3])}
            for r in cur.fetchall()
        ]
    return json.dumps({"runs": runs, "count": len(runs)}).encode()


def _health() -> bytes:
    """Liveness only. Deliberately carries no counts, versions or agent names --
    a peer granted read_health is asking whether we are up, not what we are."""
    return json.dumps({"status": "ok"}).encode()


async def _trigger(
    payload: dict[str, Any],
    runner: Any,
    *,
    principal_id: str,
    principal_role: str,
    tenant_id: str,
    peer_name: str = "",
) -> bytes:
    from robothor.engine.models import TriggerType

    agent_id = payload.get("agent_id")
    if not agent_id:
        return _err("agent_id required")

    identity = None
    try:
        from robothor.identity.context import IdentityContext

        identity = IdentityContext(
            tenant_id=tenant_id,
            channel="federation",
            identifier=principal_id,
            verified=True,
            display_name=peer_name or principal_id,
            role=principal_role,
        )
    except Exception:  # noqa: BLE001 - identity is enrichment, not the gate
        logger.debug("could not build federation IdentityContext")

    kwargs: dict[str, Any] = {
        "agent_id": agent_id,
        "message": payload.get("message", ""),
        "trigger_type": TriggerType.FEDERATION,
        "trigger_detail": f"federation:{principal_id}",
        # The three that were missing. Without user_role, runner.py falls back
        # to the allow-all `service` role for system triggers.
        "user_id": principal_id,
        "user_role": principal_role,
        "tenant_id": tenant_id,
    }
    if identity is not None:
        kwargs["identity"] = identity

    run = await runner.execute(**kwargs)
    return json.dumps({"triggered": True, "run_id": getattr(run, "id", None)}).encode()

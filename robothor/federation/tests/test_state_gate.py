"""Only the handshake runs before activation, and nothing runs after suspension.

Two gaps this closes:

1. The responder gated on capability, authorization and tenancy — but not on
   connection STATE. A PENDING row that reached the wire would have served
   real ops, which turns "activation" into paperwork.

2. Pairing needs the parent to be listening before the child is activated,
   because the child's hello is what activates it. So a pending connection
   must be reachable for exactly one op and no others. If that restriction
   lived in the transport as well as the responder there would be two places
   to get it right; it lives in the responder, and the transport simply
   declines to attach pending connections unless asked to.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.federation_responder import make_command_handler
from robothor.federation.models import ConnectionState


def _conn(state, *, exports=("read_health", "read_runs", "trigger_agent")):
    return SimpleNamespace(
        id="conn-1",
        exports=list(exports),
        state=state,
        local_principal_id="federation:conn-1",
        local_principal_role="federation_parent",
        tenant_id="default",
        peer_name="probe",
        peer_public_key="",
        metadata={},
    )


async def _call(conn, op, runner=None):
    handler = make_command_handler(conn, runner)
    with patch("robothor.engine.permissions.check_tool_permission", return_value=None):
        return json.loads(await handler(json.dumps({"op": op}).encode()))


# ── Before activation ────────────────────────────────────────────────


@pytest.mark.parametrize("op", ["health", "list_runs", "trigger"])
async def test_a_pending_connection_serves_nothing_but_the_handshake(op):
    runner = SimpleNamespace(execute=AsyncMock())
    reply = await _call(_conn(ConnectionState.PENDING), op, runner)

    assert "error" in reply, f"op {op!r} was served on a PENDING connection"
    assert "not active" in reply["error"].lower()
    runner.execute.assert_not_awaited()


async def test_the_handshake_op_is_reachable_while_pending():
    """Not that it succeeds — an empty payload is a bad handshake — but that
    it is dispatched rather than refused for state. If it were refused, no
    connection could ever activate."""
    reply = await _call(_conn(ConnectionState.PENDING), "handshake")

    assert "not active" not in reply.get("error", "").lower()


# ── After suspension ─────────────────────────────────────────────────


@pytest.mark.parametrize("op", ["health", "list_runs", "trigger"])
async def test_a_suspended_connection_serves_nothing(op):
    """Suspending a child is the operator's kill switch. It has to actually
    stop traffic that is already flowing, not just change a status string."""
    runner = SimpleNamespace(execute=AsyncMock())
    reply = await _call(_conn(ConnectionState.SUSPENDED), op, runner)

    assert "not active" in reply.get("error", "").lower()
    runner.execute.assert_not_awaited()


async def test_a_suspended_connection_cannot_handshake_its_way_back():
    """Otherwise the kill switch lasts exactly as long as it takes the peer to
    resend its hello."""
    reply = await _call(_conn(ConnectionState.SUSPENDED), "handshake")

    assert "error" in reply
    assert "suspend" in reply["error"].lower() or "not active" in reply["error"].lower()


# ── After activation ─────────────────────────────────────────────────


async def test_an_active_connection_serves_its_exported_ops():
    reply = await _call(_conn(ConnectionState.ACTIVE), "health")
    assert "error" not in reply, reply


async def test_replaying_a_handshake_at_an_active_connection_is_refused():
    """Re-pairing an established link must go through the operator, not through
    whoever still holds the invite."""
    reply = await _call(_conn(ConnectionState.ACTIVE), "handshake")

    assert "error" in reply
    assert "already" in reply["error"].lower()


# ── A connection with no state at all ────────────────────────────────


async def test_a_connection_with_no_state_attribute_is_treated_as_inactive():
    """Fail closed. A row shape this code does not recognise is not a licence."""
    conn = SimpleNamespace(
        id="conn-1",
        exports=["read_health"],
        local_principal_id="federation:conn-1",
        local_principal_role="federation_parent",
        tenant_id="default",
        peer_name="probe",
    )
    handler = make_command_handler(conn, None)
    with patch("robothor.engine.permissions.check_tool_permission", return_value=None):
        reply = json.loads(await handler(json.dumps({"op": "health"}).encode()))

    assert "error" in reply

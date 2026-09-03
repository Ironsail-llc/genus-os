"""Inbound federation responder — authorize + execute peer ops."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from robothor.engine.federation_responder import make_command_handler


def _conn(exports, *, role="federation_parent", tenant="default", state="active"):
    """A connection now carries a PRINCIPAL, not just a capability list.

    Before 2026-08-27 an inbound op executed with no identity: runner.execute()
    got no user_id/user_role/tenant_id, so a system trigger fell back to the
    allow-all `service` role. `local_principal_role` is what closes that, and
    it defaults to the deny-all `federation_child` when absent.
    """
    return SimpleNamespace(
        id="conn-1",
        exports=exports,
        local_principal_id="federation:conn-1",
        local_principal_role=role,
        tenant_id=tenant,
        peer_name="probe",
        # A connection only carries traffic once the handshake has activated
        # it. Gate 0 reads this on every call, and a connection with no state
        # at all is treated as inactive — see
        # robothor/federation/tests/test_state_gate.py.
        state=state,
    )


async def test_denied_when_capability_not_exported():
    handler = make_command_handler(_conn(exports=["read_health"]), runner=None)
    reply = json.loads(await handler(json.dumps({"op": "trigger", "agent_id": "main"}).encode()))
    assert "error" in reply and "not authorized" in reply["error"]


async def test_unknown_op_rejected():
    handler = make_command_handler(_conn(exports=["read_runs"]), runner=None)
    reply = json.loads(await handler(json.dumps({"op": "bogus"}).encode()))
    assert "unknown op" in reply["error"]


async def test_malformed_request():
    handler = make_command_handler(_conn(exports=["read_runs"]), runner=None)
    reply = json.loads(await handler(b"not json"))
    assert "malformed" in reply["error"]


async def test_trigger_executes_when_exported():
    """Dispatch only. The authorization gate is patched OPEN here so this tests
    what it says it tests; that gate has its own tests in
    robothor/federation/tests/test_asymmetry.py, and
    `test_the_authorization_gate_is_actually_consulted` below proves it is not
    simply absent."""
    runner = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(id="run-9")))
    handler = make_command_handler(_conn(exports=["trigger_agent"]), runner=runner)
    with patch("robothor.engine.permissions.check_tool_permission", return_value=None):
        reply = json.loads(
            await handler(
                json.dumps({"op": "trigger", "agent_id": "main", "message": "go"}).encode()
            )
        )
    assert reply == {"triggered": True, "run_id": "run-9"}
    runner.execute.assert_awaited_once()

    # The three arguments whose absence made every federation trigger run as
    # the allow-all `service` role.
    kwargs = runner.execute.await_args.kwargs
    assert kwargs["user_id"] == "federation:conn-1"
    assert kwargs["user_role"] == "federation_parent"
    assert kwargs["tenant_id"] == "default"


async def test_the_authorization_gate_is_actually_consulted():
    """If someone deletes the check_tool_permission call, the tests above still
    pass because they patch it. This one fails."""
    runner = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(id="r")))
    handler = make_command_handler(_conn(exports=["trigger_agent"]), runner=runner)
    with patch("robothor.engine.permissions.check_tool_permission", return_value="nope") as gate:
        reply = json.loads(
            await handler(json.dumps({"op": "trigger", "agent_id": "main"}).encode())
        )
    gate.assert_called_once()
    assert "denied" in reply.get("error", ""), reply
    runner.execute.assert_not_awaited()


async def test_list_runs_queries_when_exported():
    handler = make_command_handler(_conn(exports=["read_runs"]), runner=None)
    cur = MagicMock()
    cur.fetchall.return_value = [("r1", "main", "completed", "2026-07-02")]
    conn = MagicMock()
    conn.cursor.return_value = cur

    class _CM:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False

    # The responder now opens THREE connections per op: the permission gate, the
    # tenant scope, and the query itself. Patch the first two so this test still
    # tests what it claims to (the query), rather than silently exercising a
    # MagicMock cursor inside check_tool_permission.
    import contextlib

    with (
        patch("robothor.db.connection.get_connection", return_value=_CM()),
        patch("robothor.engine.permissions.check_tool_permission", return_value=None),
        patch("robothor.db.connection.tenant_scope", lambda _t: contextlib.nullcontext()),
    ):
        reply = json.loads(
            await handler(json.dumps({"op": "list_runs", "agent_id": "main"}).encode())
        )
    assert reply["count"] == 1 and reply["runs"][0]["run_id"] == "r1"

    # The tenant predicate that was missing: _list_runs used to SELECT across
    # every tenant with no WHERE clause at all.
    sql, params = cur.execute.call_args.args
    assert "tenant_id = %s" in sql, f"no tenant predicate in the query: {sql!r}"
    assert params[0] == "default"

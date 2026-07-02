"""Inbound federation responder — authorize + execute peer ops."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from robothor.engine.federation_responder import make_command_handler


def _conn(exports):
    return SimpleNamespace(id="conn-1", exports=exports)


async def test_denied_when_capability_not_exported():
    handler = make_command_handler(_conn(exports=["health"]), runner=None)
    reply = json.loads(await handler(json.dumps({"op": "trigger", "agent_id": "main"}).encode()))
    assert "error" in reply and "not authorized" in reply["error"]


async def test_unknown_op_rejected():
    handler = make_command_handler(_conn(exports=["agent_runs"]), runner=None)
    reply = json.loads(await handler(json.dumps({"op": "bogus"}).encode()))
    assert "unknown op" in reply["error"]


async def test_malformed_request():
    handler = make_command_handler(_conn(exports=["agent_runs"]), runner=None)
    reply = json.loads(await handler(b"not json"))
    assert "malformed" in reply["error"]


async def test_trigger_executes_when_exported():
    runner = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(id="run-9")))
    handler = make_command_handler(_conn(exports=["agent_runs"]), runner=runner)
    reply = json.loads(
        await handler(json.dumps({"op": "trigger", "agent_id": "main", "message": "go"}).encode())
    )
    assert reply == {"triggered": True, "run_id": "run-9"}
    runner.execute.assert_awaited_once()


async def test_list_runs_queries_when_exported():
    handler = make_command_handler(_conn(exports=["agent_runs"]), runner=None)
    cur = MagicMock()
    cur.fetchall.return_value = [("r1", "main", "completed", "2026-07-02")]
    conn = MagicMock()
    conn.cursor.return_value = cur

    class _CM:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False

    with patch("robothor.db.connection.get_connection", return_value=_CM()):
        reply = json.loads(
            await handler(json.dumps({"op": "list_runs", "agent_id": "main"}).encode())
        )
    assert reply["count"] == 1 and reply["runs"][0]["run_id"] == "r1"

"""Tests for federation tool handlers — mock DB layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.tools.constants import FEDERATION_TOOLS, READONLY_TOOLS
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.federation import (
    _federation_query,
    _federation_sync_status,
    _federation_trigger,
)
from robothor.federation.models import Connection, ConnectionState, Relationship


@pytest.fixture()
def ctx():
    return ToolContext(agent_id="main", tenant_id="test-tenant", workspace="/home/test")


def _mock_load_connections(connections: list[Connection]):
    """Return a patcher that mocks load_connections to return given list.

    Lazy imports inside _run() closures import from the source module,
    so we must patch at robothor.federation.connections.load_connections.
    """
    return patch(
        "robothor.federation.connections.load_connections",
        return_value=connections,
    )


def _active_conn(conn_id: str = "conn-1", peer_name: str = "Peer") -> Connection:
    return Connection(
        id=conn_id,
        peer_id="peer-1",
        peer_name=peer_name,
        state=ConnectionState.ACTIVE,
        relationship=Relationship.PEER,
        exports=["read_health"],
        # 2026-08-27: was ["memory_search", "agent_runs"]. `agent_runs`
        # covered BOTH list_runs and trigger, which is what let a child
        # execute on its parent by default. Reading and executing are now
        # separate capabilities and this fixture negotiates both explicitly.
        imports=["search_memory", "read_runs", "trigger_agent"],
    )


def _pending_conn(conn_id: str = "conn-2") -> Connection:
    return Connection(
        id=conn_id,
        peer_id="peer-2",
        peer_name="Pending",
        state=ConnectionState.PENDING,
    )


# ── Constants ──────────────────────────────────────────────────────────


class TestFederationConstants:
    def test_federation_tools_defined(self):
        assert "federation_query" in FEDERATION_TOOLS
        assert "federation_trigger" in FEDERATION_TOOLS
        assert "federation_sync_status" in FEDERATION_TOOLS

    def test_readonly_tools_include_query_and_status(self):
        assert "federation_query" in READONLY_TOOLS
        assert "federation_sync_status" in READONLY_TOOLS

    def test_trigger_not_readonly(self):
        assert "federation_trigger" not in READONLY_TOOLS


# ── federation_query ───────────────────────────────────────────────────


class TestFederationQuery:
    @pytest.mark.asyncio
    async def test_query_missing_connection_id(self, ctx):
        result = await _federation_query({}, ctx)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_query_health(self, ctx):
        with _mock_load_connections([_active_conn()]):
            result = await _federation_query(
                {"connection_id": "conn-1", "query_type": "health"}, ctx
            )
        assert result["peer_name"] == "Peer"
        assert result["state"] == "active"
        assert "read_health" in result["exports"]

    @pytest.mark.asyncio
    async def test_query_runs_dispatches_over_nats(self, ctx):
        # query_type=runs crosses the wire via the NATS request transport.
        with (
            _mock_load_connections([_active_conn()]),
            patch(
                "robothor.engine.tools.handlers.federation._federation_request",
                new_callable=AsyncMock,
                return_value={"runs": []},
            ) as req,
        ):
            result = await _federation_query(
                {"connection_id": "conn-1", "query_type": "runs", "agent_id": "main", "limit": 5},
                ctx,
            )
        assert result == {"runs": []}
        payload = req.call_args.args[1]
        assert payload["op"] == "list_runs"
        assert payload["agent_id"] == "main"
        assert payload["limit"] == 5

    @pytest.mark.asyncio
    async def test_query_runs_denied_without_capability(self, ctx):
        # A live connection that did NOT negotiate 'agent_runs' cannot list runs.
        conn = _active_conn()
        conn.imports = ["search_memory"]  # no read_runs
        with _mock_load_connections([conn]):
            result = await _federation_query({"connection_id": "conn-1", "query_type": "runs"}, ctx)
        assert "error" in result
        assert "not authorized" in result["error"]

    @pytest.mark.asyncio
    async def test_query_unknown_type(self, ctx):
        with _mock_load_connections([_active_conn()]):
            result = await _federation_query(
                {"connection_id": "conn-1", "query_type": "bogus"}, ctx
            )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_query_connection_not_found(self, ctx):
        with _mock_load_connections([]):
            result = await _federation_query({"connection_id": "nope", "query_type": "health"}, ctx)
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_query_connection_not_active(self, ctx):
        with _mock_load_connections([_pending_conn("conn-2")]):
            result = await _federation_query(
                {"connection_id": "conn-2", "query_type": "health"}, ctx
            )
        assert "error" in result
        assert "not active" in result["error"]


# ── federation_trigger ─────────────────────────────────────────────────


class TestFederationTrigger:
    @pytest.mark.asyncio
    async def test_trigger_missing_fields(self, ctx):
        result = await _federation_trigger({}, ctx)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_trigger_dispatches_over_nats(self, ctx):
        with (
            _mock_load_connections([_active_conn()]),
            patch(
                "robothor.engine.tools.handlers.federation._federation_request",
                new_callable=AsyncMock,
                return_value={"triggered": True},
            ) as req,
        ):
            result = await _federation_trigger(
                {"connection_id": "conn-1", "agent_id": "email-classifier", "message": "run now"},
                ctx,
            )
        assert result == {"triggered": True}
        payload = req.call_args.args[1]
        assert payload["op"] == "trigger"
        assert payload["agent_id"] == "email-classifier"
        assert payload["message"] == "run now"

    @pytest.mark.asyncio
    async def test_trigger_denied_without_capability(self, ctx):
        conn = _active_conn()
        conn.imports = ["search_memory"]  # no trigger_agent
        with _mock_load_connections([conn]):
            result = await _federation_trigger(
                {"connection_id": "conn-1", "agent_id": "main", "message": "go"}, ctx
            )
        assert "error" in result
        assert "not authorized" in result["error"]

    @pytest.mark.asyncio
    async def test_trigger_connection_not_active(self, ctx):
        with _mock_load_connections([_pending_conn("conn-2")]):
            result = await _federation_trigger({"connection_id": "conn-2", "agent_id": "main"}, ctx)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_trigger_forwards_full_message(self, ctx):
        with (
            _mock_load_connections([_active_conn()]),
            patch(
                "robothor.engine.tools.handlers.federation._federation_request",
                new_callable=AsyncMock,
                return_value={"ok": True},
            ) as req,
        ):
            await _federation_trigger(
                {"connection_id": "conn-1", "agent_id": "main", "message": "x" * 500},
                ctx,
            )
        assert req.call_args.args[1]["message"] == "x" * 500


# ── federation_sync_status ─────────────────────────────────────────────


class TestFederationSyncStatus:
    @pytest.mark.asyncio
    async def test_sync_status_single(self, ctx):
        with (
            _mock_load_connections([_active_conn()]),
            patch("robothor.federation.sync.EventJournal") as mock_journal,
        ):
            journal = mock_journal.return_value
            journal.get_sync_watermark.return_value = "1000:0:a"
            journal.get_unsynced.return_value = []

            result = await _federation_sync_status({"connection_id": "conn-1"}, ctx)

        assert len(result["connections"]) == 1
        status = result["connections"][0]
        assert status["connection_id"] == "conn-1"
        assert status["state"] == "active"

    @pytest.mark.asyncio
    async def test_sync_status_all(self, ctx):
        with (
            _mock_load_connections([_active_conn("c1"), _pending_conn("c2")]),
            patch("robothor.federation.sync.EventJournal") as mock_journal,
        ):
            journal = mock_journal.return_value
            journal.get_sync_watermark.return_value = ""
            journal.get_unsynced.return_value = []

            result = await _federation_sync_status({}, ctx)

        assert len(result["connections"]) == 2

    @pytest.mark.asyncio
    async def test_sync_status_not_found(self, ctx):
        with _mock_load_connections([]):
            result = await _federation_sync_status({"connection_id": "nope"}, ctx)
        assert "error" in result


class TestReadingDoesNotImplyExecuting:
    """The distinction the old single-capability model could not express.

    Until 2026-08-27 `list_runs` and `trigger` both required `agent_runs`, so
    granting a peer the right to SEE what an instance had done also granted the
    right to make it do something new. A connection cannot be in this state any
    more, and this is the test that says so.
    """

    @pytest.mark.asyncio
    async def test_a_peer_granted_reads_cannot_trigger(self, ctx):
        conn = _active_conn()
        conn.imports = ["read_runs"]  # reads yes, execute no
        with (
            _mock_load_connections([conn]),
            patch(
                "robothor.engine.tools.handlers.federation._federation_request",
                new_callable=AsyncMock,
                return_value={"runs": []},
            ),
        ):
            listed = await _federation_query({"connection_id": "conn-1", "query_type": "runs"}, ctx)
            triggered = await _federation_trigger(
                {"connection_id": "conn-1", "agent_id": "main", "message": "go"}, ctx
            )
        assert "error" not in listed, f"read was refused: {listed}"
        assert "error" in triggered and "not authorized" in triggered["error"], (
            "a peer granted only read_runs was allowed to trigger — reading and "
            "executing have collapsed back into one capability"
        )

"""Tests for federation tool handlers — mock DB layer."""

from __future__ import annotations

from unittest.mock import patch

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
    return ToolContext(agent_id="main", tenant_id="robothor-primary", workspace="/home/test")


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
        exports=["health", "config_push"],
        imports=["memory_search", "agent_runs"],
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
        assert "health" in result["exports"]

    @pytest.mark.asyncio
    async def test_query_runs(self, ctx):
        with _mock_load_connections([_active_conn()]):
            result = await _federation_query(
                {"connection_id": "conn-1", "query_type": "runs", "agent_id": "main", "limit": 5},
                ctx,
            )
        assert result["agent_id"] == "main"
        assert result["limit"] == 5

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
    async def test_trigger_success(self, ctx):
        with _mock_load_connections([_active_conn()]):
            result = await _federation_trigger(
                {"connection_id": "conn-1", "agent_id": "email-classifier", "message": "run now"},
                ctx,
            )
        assert result["agent_id"] == "email-classifier"
        assert result["peer_name"] == "Peer"

    @pytest.mark.asyncio
    async def test_trigger_connection_not_active(self, ctx):
        with _mock_load_connections([_pending_conn("conn-2")]):
            result = await _federation_trigger({"connection_id": "conn-2", "agent_id": "main"}, ctx)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_trigger_truncates_message(self, ctx):
        with _mock_load_connections([_active_conn()]):
            result = await _federation_trigger(
                {"connection_id": "conn-1", "agent_id": "main", "message": "x" * 500},
                ctx,
            )
        assert len(result["message"]) == 200


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

"""Tests for ConnectionManager — state machine, exports/imports, list/filter."""

from __future__ import annotations

import pytest

from robothor.federation.connections import _STATE_TRANSITIONS, ConnectionManager
from robothor.federation.models import (
    Connection,
    ConnectionState,
    Relationship,
)


@pytest.fixture()
def mgr():
    return ConnectionManager()


@pytest.fixture()
def active_conn():
    return Connection(
        id="conn-1",
        peer_id="peer-1",
        peer_name="Test Peer",
        state=ConnectionState.ACTIVE,
        relationship=Relationship.PEER,
        exports=["health", "agent_runs"],
        imports=["memory_search"],
    )


@pytest.fixture()
def pending_conn():
    return Connection(
        id="conn-2",
        peer_id="peer-2",
        peer_name="Pending Peer",
        state=ConnectionState.PENDING,
        relationship=Relationship.CHILD,
    )


class TestConnectionManagerBasics:
    def test_add_and_get(self, mgr, active_conn):
        mgr.add(active_conn)
        assert mgr.get("conn-1") is active_conn

    def test_get_nonexistent(self, mgr):
        assert mgr.get("nonexistent") is None

    def test_get_by_peer(self, mgr, active_conn):
        mgr.add(active_conn)
        assert mgr.get_by_peer("peer-1") is active_conn

    def test_get_by_peer_nonexistent(self, mgr):
        assert mgr.get_by_peer("unknown") is None

    def test_list_all(self, mgr, active_conn, pending_conn):
        mgr.add(active_conn)
        mgr.add(pending_conn)
        assert len(mgr.list_all()) == 2

    def test_list_active(self, mgr, active_conn, pending_conn):
        mgr.add(active_conn)
        mgr.add(pending_conn)
        active_list = mgr.list_active()
        assert len(active_list) == 1
        assert active_list[0].id == "conn-1"

    def test_remove(self, mgr, active_conn):
        mgr.add(active_conn)
        assert mgr.remove("conn-1") is True
        assert mgr.get("conn-1") is None

    def test_remove_nonexistent(self, mgr):
        assert mgr.remove("nope") is False


class TestStateTransitions:
    def test_valid_transitions_defined(self):
        """Every state has at least one valid next state."""
        for state in ConnectionState:
            assert state in _STATE_TRANSITIONS

    def test_pending_to_active(self, mgr, pending_conn):
        mgr.add(pending_conn)
        conn = mgr.activate("conn-2")
        assert conn.state == ConnectionState.ACTIVE
        assert conn.updated_at  # timestamp set

    def test_pending_to_suspended(self, mgr, pending_conn):
        mgr.add(pending_conn)
        conn = mgr.suspend("conn-2")
        assert conn.state == ConnectionState.SUSPENDED

    def test_active_to_limited(self, mgr, active_conn):
        mgr.add(active_conn)
        conn = mgr.limit("conn-1")
        assert conn.state == ConnectionState.LIMITED

    def test_active_to_suspended(self, mgr, active_conn):
        mgr.add(active_conn)
        conn = mgr.suspend("conn-1")
        assert conn.state == ConnectionState.SUSPENDED

    def test_limited_to_active(self, mgr):
        conn = Connection(id="c", state=ConnectionState.LIMITED)
        mgr.add(conn)
        result = mgr.activate("c")
        assert result.state == ConnectionState.ACTIVE

    def test_limited_to_suspended(self, mgr):
        conn = Connection(id="c", state=ConnectionState.LIMITED)
        mgr.add(conn)
        result = mgr.suspend("c")
        assert result.state == ConnectionState.SUSPENDED

    def test_suspended_to_active(self, mgr):
        conn = Connection(id="c", state=ConnectionState.SUSPENDED)
        mgr.add(conn)
        result = mgr.activate("c")
        assert result.state == ConnectionState.ACTIVE

    def test_suspended_to_pending(self, mgr):
        conn = Connection(id="c", state=ConnectionState.SUSPENDED)
        mgr.add(conn)
        result = mgr.transition_state("c", ConnectionState.PENDING)
        assert result.state == ConnectionState.PENDING

    def test_invalid_transition_raises(self, mgr, pending_conn):
        """Cannot go directly from PENDING to LIMITED."""
        mgr.add(pending_conn)
        with pytest.raises(ValueError, match="Cannot transition"):
            mgr.limit("conn-2")

    def test_active_to_pending_raises(self, mgr, active_conn):
        """Cannot go from ACTIVE back to PENDING."""
        mgr.add(active_conn)
        with pytest.raises(ValueError, match="Cannot transition"):
            mgr.transition_state("conn-1", ConnectionState.PENDING)

    def test_transition_nonexistent_raises(self, mgr):
        with pytest.raises(ValueError, match="not found"):
            mgr.transition_state("nope", ConnectionState.ACTIVE)


class TestExportsImports:
    def test_set_exports(self, mgr, active_conn):
        mgr.add(active_conn)
        conn = mgr.set_exports("conn-1", ["a", "b"])
        assert conn.exports == ["a", "b"]

    def test_add_export(self, mgr, active_conn):
        mgr.add(active_conn)
        conn = mgr.add_export("conn-1", "new_cap")
        assert "new_cap" in conn.exports
        assert "health" in conn.exports  # existing preserved

    def test_add_export_idempotent(self, mgr, active_conn):
        mgr.add(active_conn)
        mgr.add_export("conn-1", "health")
        assert active_conn.exports.count("health") == 1

    def test_remove_export(self, mgr, active_conn):
        mgr.add(active_conn)
        conn = mgr.remove_export("conn-1", "health")
        assert "health" not in conn.exports

    def test_remove_export_not_present(self, mgr, active_conn):
        """Removing a non-existent export is a no-op."""
        mgr.add(active_conn)
        old_updated = active_conn.updated_at
        conn = mgr.remove_export("conn-1", "nonexistent")
        assert conn.updated_at == old_updated

    def test_set_imports(self, mgr, active_conn):
        mgr.add(active_conn)
        conn = mgr.set_imports("conn-1", ["x", "y"])
        assert conn.imports == ["x", "y"]

    def test_is_exported_active(self, mgr, active_conn):
        mgr.add(active_conn)
        assert mgr.is_exported("conn-1", "health") is True
        assert mgr.is_exported("conn-1", "nonexistent") is False

    def test_is_exported_inactive(self, mgr, pending_conn):
        """is_exported returns False if connection is not ACTIVE."""
        pending_conn.exports = ["health"]
        mgr.add(pending_conn)
        assert mgr.is_exported("conn-2", "health") is False

    def test_is_imported_active(self, mgr, active_conn):
        mgr.add(active_conn)
        assert mgr.is_imported("conn-1", "memory_search") is True
        assert mgr.is_imported("conn-1", "nope") is False

    def test_is_imported_nonexistent(self, mgr):
        assert mgr.is_imported("nope", "any") is False

    def test_exports_nonexistent_raises(self, mgr):
        with pytest.raises(ValueError, match="not found"):
            mgr.set_exports("nope", [])

    def test_add_export_nonexistent_raises(self, mgr):
        with pytest.raises(ValueError, match="not found"):
            mgr.add_export("nope", "x")

    def test_remove_export_nonexistent_raises(self, mgr):
        with pytest.raises(ValueError, match="not found"):
            mgr.remove_export("nope", "x")

    def test_set_imports_nonexistent_raises(self, mgr):
        with pytest.raises(ValueError, match="not found"):
            mgr.set_imports("nope", [])

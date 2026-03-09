"""Connection management — state transitions, export/import configuration, persistence."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from robothor.federation.models import (
    Connection,
    ConnectionState,
    Relationship,
)

logger = logging.getLogger(__name__)

# Valid state transitions (current_state → allowed next states)
_STATE_TRANSITIONS: dict[ConnectionState, set[ConnectionState]] = {
    ConnectionState.PENDING: {ConnectionState.ACTIVE, ConnectionState.SUSPENDED},
    ConnectionState.ACTIVE: {ConnectionState.LIMITED, ConnectionState.SUSPENDED},
    ConnectionState.LIMITED: {ConnectionState.ACTIVE, ConnectionState.SUSPENDED},
    ConnectionState.SUSPENDED: {ConnectionState.ACTIVE, ConnectionState.PENDING},
}


class ConnectionManager:
    """Manages federation connections.

    Wraps the database layer — all state mutations go through here.
    """

    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}

    def add(self, connection: Connection) -> None:
        """Register a new connection."""
        self._connections[connection.id] = connection

    def get(self, connection_id: str) -> Connection | None:
        """Get a connection by ID."""
        return self._connections.get(connection_id)

    def get_by_peer(self, peer_id: str) -> Connection | None:
        """Get a connection by peer instance ID."""
        for conn in self._connections.values():
            if conn.peer_id == peer_id:
                return conn
        return None

    def list_all(self) -> list[Connection]:
        """List all connections."""
        return list(self._connections.values())

    def list_active(self) -> list[Connection]:
        """List active connections only."""
        return [c for c in self._connections.values() if c.state == ConnectionState.ACTIVE]

    def transition_state(
        self,
        connection_id: str,
        new_state: ConnectionState,
    ) -> Connection:
        """Transition a connection to a new state.

        Raises ValueError if the transition is not allowed.
        """
        conn = self._connections.get(connection_id)
        if not conn:
            raise ValueError(f"Connection not found: {connection_id}")

        allowed = _STATE_TRANSITIONS.get(conn.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Cannot transition from {conn.state} to {new_state}. "
                f"Allowed: {', '.join(s.value for s in allowed)}"
            )

        conn.state = new_state
        conn.updated_at = datetime.now(UTC).isoformat()
        return conn

    def activate(self, connection_id: str) -> Connection:
        """Activate a connection (pending/limited/suspended → active)."""
        return self.transition_state(connection_id, ConnectionState.ACTIVE)

    def suspend(self, connection_id: str) -> Connection:
        """Suspend a connection."""
        return self.transition_state(connection_id, ConnectionState.SUSPENDED)

    def limit(self, connection_id: str) -> Connection:
        """Put a connection into limited mode."""
        return self.transition_state(connection_id, ConnectionState.LIMITED)

    def remove(self, connection_id: str) -> bool:
        """Remove a connection entirely."""
        return self._connections.pop(connection_id, None) is not None

    def set_exports(self, connection_id: str, exports: list[str]) -> Connection:
        """Set what this instance exposes to the peer."""
        conn = self._connections.get(connection_id)
        if not conn:
            raise ValueError(f"Connection not found: {connection_id}")
        conn.exports = list(exports)
        conn.updated_at = datetime.now(UTC).isoformat()
        return conn

    def add_export(self, connection_id: str, capability: str) -> Connection:
        """Add a single export capability."""
        conn = self._connections.get(connection_id)
        if not conn:
            raise ValueError(f"Connection not found: {connection_id}")
        if capability not in conn.exports:
            conn.exports.append(capability)
            conn.updated_at = datetime.now(UTC).isoformat()
        return conn

    def remove_export(self, connection_id: str, capability: str) -> Connection:
        """Remove a single export capability."""
        conn = self._connections.get(connection_id)
        if not conn:
            raise ValueError(f"Connection not found: {connection_id}")
        if capability in conn.exports:
            conn.exports.remove(capability)
            conn.updated_at = datetime.now(UTC).isoformat()
        return conn

    def set_imports(self, connection_id: str, imports: list[str]) -> Connection:
        """Set what this instance consumes from the peer."""
        conn = self._connections.get(connection_id)
        if not conn:
            raise ValueError(f"Connection not found: {connection_id}")
        conn.imports = list(imports)
        conn.updated_at = datetime.now(UTC).isoformat()
        return conn

    def is_exported(self, connection_id: str, capability: str) -> bool:
        """Check if a capability is exported to the given peer."""
        conn = self._connections.get(connection_id)
        if not conn or conn.state != ConnectionState.ACTIVE:
            return False
        return capability in conn.exports

    def is_imported(self, connection_id: str, capability: str) -> bool:
        """Check if a capability is imported from the given peer."""
        conn = self._connections.get(connection_id)
        if not conn or conn.state != ConnectionState.ACTIVE:
            return False
        return capability in conn.imports


# ── Database persistence ──────────────────────────────────────────────


def save_connection(conn: Connection) -> None:
    """Persist a connection to the database."""
    import json

    from robothor.db.connection import get_connection

    with get_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO federation_connections
                (id, peer_id, peer_name, peer_endpoint, peer_public_key,
                 relationship, state, exports, imports, nats_account,
                 metadata, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                state = EXCLUDED.state,
                exports = EXCLUDED.exports,
                imports = EXCLUDED.imports,
                nats_account = EXCLUDED.nats_account,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
            """,
            (
                conn.id,
                conn.peer_id,
                conn.peer_name,
                conn.peer_endpoint,
                conn.peer_public_key,
                conn.relationship.value,
                conn.state.value,
                json.dumps(conn.exports),
                json.dumps(conn.imports),
                conn.nats_account,
                json.dumps(conn.metadata),
                conn.created_at,
                conn.updated_at,
            ),
        )
        db.commit()


def load_connections() -> list[Connection]:
    """Load all connections from the database."""
    import json

    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT id, peer_id, peer_name, peer_endpoint, peer_public_key,
                       relationship, state, exports, imports, nats_account,
                       metadata, created_at, updated_at
                FROM federation_connections
                ORDER BY created_at
                """
            )
            rows = cur.fetchall()

        return [
            Connection(
                id=row[0],
                peer_id=row[1],
                peer_name=row[2],
                peer_endpoint=row[3],
                peer_public_key=row[4],
                relationship=Relationship(row[5]),
                state=ConnectionState(row[6]),
                exports=json.loads(row[7]) if isinstance(row[7], str) else row[7],
                imports=json.loads(row[8]) if isinstance(row[8], str) else row[8],
                nats_account=row[9] or "",
                metadata=json.loads(row[10]) if isinstance(row[10], str) else (row[10] or {}),
                created_at=str(row[11]) if row[11] else "",
                updated_at=str(row[12]) if row[12] else "",
            )
            for row in rows
        ]
    except Exception as e:
        logger.warning("Failed to load connections: %s", e)
        return []


def delete_connection(connection_id: str) -> bool:
    """Delete a connection from the database."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            cur = db.cursor()
            cur.execute("DELETE FROM federation_connections WHERE id = %s", (connection_id,))
            deleted: bool = cur.rowcount > 0
            db.commit()
        return deleted
    except Exception as e:
        logger.warning("Failed to delete connection: %s", e)
        return False

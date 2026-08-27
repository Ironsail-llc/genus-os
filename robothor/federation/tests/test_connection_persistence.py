"""A connection must survive a restart with its principal and tenant intact.

Migration 112 added tenant_id, direction, local_principal_id,
local_principal_role, transport, last_seen_at and activated_at to
federation_connections. If ``save_connection`` and ``load_connections`` do not
carry them, the columns exist and hold NULL forever, and the responder's
``_principal()`` falls back to its defaults on every restart.

Those defaults fail CLOSED (`federation_child` is seeded `'*' -> deny`), so the
symptom is not a leak — it is a parent that silently loses its own read
capabilities the first time the daemon restarts. That is precisely the class of
bug this box keeps shipping: correct code, and a caller that never carries the
value to it.
"""

from __future__ import annotations

import os
import uuid

import pytest

from robothor.federation.connections import (
    delete_connection,
    load_connections,
    save_connection,
)
from robothor.federation.models import (
    Connection,
    ConnectionState,
    Relationship,
)

pytestmark = pytest.mark.integration


def _db_available() -> bool:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as db:
            db.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not os.environ.get("ROBOTHOR_DB_NAME") and not _db_available(),
    reason="no database available",
)


@pytest.fixture
def saved_connection():
    made: list[str] = []

    def _make(**kwargs) -> Connection:
        conn = Connection(
            id=str(uuid.uuid4()),
            peer_id=str(uuid.uuid4()),
            peer_name="probe-child",
            peer_endpoint="nats://127.0.0.1:4222",
            peer_public_key="-----BEGIN PUBLIC KEY-----\nprobe\n-----END PUBLIC KEY-----",
            relationship=Relationship.CHILD,
            state=ConnectionState.PENDING,
            created_at="2026-08-27T00:00:00+00:00",
            updated_at="2026-08-27T00:00:00+00:00",
            **kwargs,
        )
        save_connection(conn)
        made.append(conn.id)
        return conn

    yield _make
    for cid in made:
        delete_connection(cid)


@requires_db
def test_the_principal_survives_a_restart(saved_connection):
    """The three values the authorization gate reads must come back from disk."""
    conn = saved_connection(
        tenant_id="robothor-primary",
        local_principal_id="federation:probe-1",
        local_principal_role="federation_parent",
    )

    reloaded = next((c for c in load_connections() if c.id == conn.id), None)
    assert reloaded is not None, "connection did not persist at all"

    assert reloaded.tenant_id == "robothor-primary", (
        "tenant_id was dropped in the round-trip — every inbound op from this "
        "peer would run in the default tenant"
    )
    assert reloaded.local_principal_id == "federation:probe-1"
    assert reloaded.local_principal_role == "federation_parent", (
        "the principal's role was dropped — _principal() falls back to "
        "federation_child (deny-all) and this peer loses every capability"
    )


@requires_db
def test_direction_and_transport_survive_a_restart(saved_connection):
    conn = saved_connection(
        direction="inbound",
        transport={"kind": "nats", "url": "nats://127.0.0.1:4222", "account": "fed_probe"},
    )

    reloaded = next((c for c in load_connections() if c.id == conn.id), None)
    assert reloaded is not None
    assert reloaded.direction == "inbound"
    assert reloaded.transport["kind"] == "nats"
    assert reloaded.transport["account"] == "fed_probe"


@requires_db
def test_activation_timestamps_survive_a_restart(saved_connection):
    """`federation status` claims a connection is active; these are what let it
    say WHEN, and whether the transport has been heard from since."""
    conn = saved_connection(
        activated_at="2026-08-27T12:00:00+00:00",
        last_seen_at="2026-08-27T12:30:00+00:00",
    )

    reloaded = next((c for c in load_connections() if c.id == conn.id), None)
    assert reloaded is not None
    assert reloaded.activated_at.startswith("2026-08-27")
    assert reloaded.last_seen_at.startswith("2026-08-27")


@requires_db
def test_an_unset_principal_falls_back_to_deny_all(saved_connection):
    """A row written before migration 112 has NULLs. The fallback must be the
    deny-all role, never the allow-all `service` role that started all this."""
    from robothor.engine.federation_responder import _principal

    conn = saved_connection()
    reloaded = next((c for c in load_connections() if c.id == conn.id), None)
    assert reloaded is not None

    _pid, role, tenant = _principal(reloaded)
    assert role == "federation_child", f"unset role resolved to {role!r}, not deny-all"
    assert tenant == "default"

"""The triage inbox's escalation lookup against a real ``crm_tasks`` table.

Unit tests feed :class:`~robothor.workspace.ingest.triage.TriageInbox` a fake
lookup; this proves the query itself: the tenant's escalation tasks that are
open or resolved within the window, their ``threadId:`` lines (Gmail hex ids
and Exchange base64 conversation ids alike), and nothing of another tenant's.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

THROUGH = "149_workspace_sync_state"


@pytest.fixture
def scratch(scratch_db):
    import psycopg2

    db, dsn = scratch_db(through=THROUGH)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES "
            "('tenant-a', 'A'), ('tenant-b', 'B') ON CONFLICT DO NOTHING"
        )
    return db, connect


def _task(
    db, tenant: str, body: str, *, tags=("escalation",), resolved: str | None = None, deleted=False
):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tasks (id, title, body, status, tags, tenant_id, resolved_at, deleted_at) "
            "VALUES (%s, 'Escalation', %s, 'TODO', %s, %s, "
            + ("now() - %s::interval" if resolved else "%s")
            + ", "
            + ("now()" if deleted else "NULL")
            + ")",
            (str(uuid.uuid4()), body, list(tags), tenant, resolved),
        )


@pytest.mark.integration
def test_open_and_recent_escalations_of_this_tenant_only(scratch):
    from robothor.workspace.ingest.triage import pg_escalation_ids

    db, connect = scratch
    exchange = "AAQkADAwATM0MDAAMS1hYjc2LTAwAi0wMAoAEABx+/abc_d-e=="
    _task(db, "tenant-a", "threadId: 19c8b019e28fbcf3\nfrom: someone")
    _task(db, "tenant-a", f"threadId: {exchange}\nfrom: someone", resolved="2 hours")
    _task(db, "tenant-a", "threadId: oldresolved1", resolved="5 days")
    _task(db, "tenant-a", "threadId: deleted00001", deleted=True)
    _task(db, "tenant-a", "threadId: notescalated", tags=("email",))
    _task(db, "tenant-a", "no thread line here")
    _task(db, "tenant-b", "threadId: othertenant1")

    ids = asyncio.run(pg_escalation_ids("tenant-a", connect=connect)())

    assert ids == ["19c8b019e28fbcf3", exchange]

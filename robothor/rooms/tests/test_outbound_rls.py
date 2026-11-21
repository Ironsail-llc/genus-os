"""An approved workspace email is delivered, and its receipt posted, with
row-level security enforcing (contract amendment 4).

Runs as ``robothor_app`` (not a superuser, which bypasses RLS) through the
real ``_apply_tenant_scope``, in a process bound to the platform tenant while
the email belongs to a child tenant: the executor must bind the action's
tenant itself, or the room is invisible and the receipt is lost.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.rls_app_role import app_role, rls_tenants  # noqa: F401 - pytest fixtures

pytestmark = pytest.mark.integration

ASSISTANT = "assistant@example.com"


def _seed(cur: Any, tenant: str) -> dict[str, str]:
    """As the superuser: Bob, his task, its workspace and his turn there."""
    bob, task, room, run = (str(uuid.uuid4()) for _ in range(4))
    key = f"agent:main:room:{room}"
    cur.execute(
        "INSERT INTO user_accounts (id, tenant_id, email, display_name, role) "
        "VALUES (%s, %s, 'bob@example.com', 'Bob', 'member')",
        (bob, tenant),
    )
    cur.execute(
        "INSERT INTO crm_tasks (id, tenant_id, title, status, assignee_id) "
        "VALUES (%s, %s, 'Renewal', 'TODO', %s)",
        (task, tenant, bob),
    )
    cur.execute(
        "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by, "
        "task_id) VALUES (%s, %s, 'task', 'Renewal', 'main', %s, %s, %s)",
        (tenant, room, key, bob, task),
    )
    cur.execute(
        "INSERT INTO room_members (tenant_id, room_id, user_id, assigned) VALUES (%s, %s, %s, TRUE)",
        (tenant, room, bob),
    )
    cur.execute(
        "INSERT INTO room_tasks (tenant_id, room_id, task_id, origin) "
        "VALUES (%s, %s, %s, 'subject')",
        (tenant, room, task),
    )
    cur.execute(
        "INSERT INTO agent_runs (id, tenant_id, agent_id, trigger_type, trigger_detail, user_id, "
        "status) VALUES (%s, %s, 'main', 'webchat', %s, %s, 'completed')",
        (run, tenant, f"webchat:{key}", bob),
    )
    return {"bob": bob, "task": task, "room": room, "run": run, "key": key}


def test_an_approved_email_of_another_tenant_is_sent_with_its_receipt_under_rls(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
    monkeypatch,
) -> None:
    from robothor.db.connection import current_tenant_scope, tenant_scope
    from robothor.events import bus
    from robothor.rooms import outbound
    from robothor.rooms.access import Actor

    tenant = rls_tenants["a"]
    with db_conn.cursor() as cur:
        seeded = _seed(cur, tenant)
    monkeypatch.setenv("ROBOTHOR_AI_EMAIL", ASSISTANT)
    monkeypatch.setattr(bus, "publish", lambda *a, **k: "1-0")
    sent: list[str] = []
    monkeypatch.setattr(
        outbound, "send_raw", lambda tenant_id, raw: sent.append(tenant_id) or {"id": "msg-1"}
    )
    bob = Actor(user_id=seeded["bob"], role="member", tenant_id=tenant)
    with tenant_scope(tenant):  # the member's own run is bound to their tenant
        proposed = outbound.propose_email(
            bob, seeded["room"], seeded["run"], to=["carol@example.org"], subject="Hi", body="Hello"
        )
    from robothor.operations.store import Operations

    Operations(tenant).decide(proposed.action_id, True, "operator:someone")

    assert current_tenant_scope() is None, "the executor runs with no scope of its own"
    assert outbound.deliver(tenant, proposed.action_id) == "sent"
    assert outbound.sweep(tenant) == 0

    assert sent == [tenant]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT cm.message FROM chat_messages cm JOIN chat_sessions s ON s.id = cm.session_id "
            "WHERE s.tenant_id = %s AND s.session_key = %s",
            (tenant, seeded["key"]),
        )
        receipts = [r[0] for r in cur.fetchall() if r[0].get("kind") == "room_email_receipt"]
        cur.execute(
            "SELECT COUNT(*) FROM operation_audit WHERE tenant_id = %s AND entity_id = %s "
            "AND event = 'room.email.noted'",
            (tenant, proposed.action_id),
        )
        noted = cur.fetchone()[0]
    assert [r["receipt"]["status"] for r in receipts] == ["sent"]
    assert noted == 1

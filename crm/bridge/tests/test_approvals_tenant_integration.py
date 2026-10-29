"""Tenant-local Inbox decisions against real rows (``*_test`` database).

The unit file proves the router asks the stores for the right tenant. This one
proves the stores, the SQL and the route agree: rows of two sibling tenants are
written for real, an admin of ``acme-child-a`` reads and decides through the
bridge, and the neighbour's rows come out untouched.

Every write goes through the ``mock_get_connection`` proxy, which holds one
connection per test and rolls it back afterwards.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHILD = "acme-child-a"
SIBLING = "acme-child-b"


@pytest.fixture
def child_admin(_controls_auth_key, mock_get_connection):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app)
    client.headers.update(
        {"Authorization": f"Bearer {tokens.issue_access_token('alice', CHILD, 'admin')}"}
    )
    return client


@pytest.fixture
def child_member(_controls_auth_key, mock_get_connection):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app)
    client.headers.update(
        {"Authorization": f"Bearer {tokens.issue_access_token('bob', CHILD, 'member')}"}
    )
    return client


def _approval(tenant: str) -> str:
    from robothor.engine.approvals import request_approval

    return str(
        request_approval(
            run_id=str(uuid.uuid4()),
            workflow_id="weekly-report",
            step_id="confirm-send",
            prompt=f"Send it for {tenant}?",
            tenant_id=tenant,
        ).id
    )


def _question(tenant: str) -> str:
    from robothor.engine.agent_questions import ask_question

    return str(
        ask_question(
            run_id=str(uuid.uuid4()),
            agent_id="assistant",
            question=f"Which vendor for {tenant}?",
            options=["Acme", "Globex"],
            tenant_id=tenant,
        ).id
    )


def _action(tenant: str) -> str:
    from robothor.operations.store import Operations

    return Operations(tenant).propose(
        "sales.email", f"draft-{uuid.uuid4()}", {"subject": f"Intro for {tenant}"}
    )


def _status(db_conn, table: str, row_id: str) -> tuple:
    who = "answered_by" if table == "agent_questions" else "decided_by"
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT status, {who}, tenant_id FROM {table} WHERE id = %s", (row_id,))
        return cur.fetchone()


def test_a_child_admin_lists_only_their_tenant(child_admin):
    mine = {_approval(CHILD), _question(CHILD), _action(CHILD)}
    theirs = {_approval(SIBLING), _question(SIBLING), _action(SIBLING)}

    body = child_admin.get("/api/approvals").json()

    listed = {item["id"] for item in body["pending"]}
    assert mine <= listed
    assert not (theirs & listed)
    assert {item["kind"] for item in body["pending"]} >= {"workflow", "question", "action"}


def test_a_child_admin_decides_their_workflow_approval(child_admin, db_conn):
    approval = _approval(CHILD)

    response = child_admin.post(f"/api/approvals/workflow/{approval}", json={"approved": True})

    assert response.status_code == 200
    assert response.json()["settled"] is True
    assert _status(db_conn, "workflow_approvals", approval) == ("approved", "operator:alice", CHILD)


def test_a_child_admin_answers_their_question(child_admin, db_conn):
    question = _question(CHILD)

    response = child_admin.post(f"/api/approvals/question/{question}", json={"answer": "Acme"})

    assert response.status_code == 200
    assert _status(db_conn, "agent_questions", question) == ("answered", "operator:alice", CHILD)


def test_a_child_admin_decides_their_proposed_action_and_it_is_audited(child_admin, db_conn):
    action = _action(CHILD)

    response = child_admin.post(f"/api/approvals/action/{action}", json={"approved": False})

    assert response.status_code == 200
    assert _status(db_conn, "operation_actions", action) == ("rejected", "operator:alice", CHILD)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, actor FROM operation_audit WHERE entity_id = %s "
            "AND event = 'action.rejected'",
            (action,),
        )
        assert cur.fetchall() == [(CHILD, "operator:alice")]


@pytest.mark.parametrize(
    ("kind", "table", "make", "body"),
    [
        ("workflow", "workflow_approvals", _approval, {"approved": True}),
        ("question", "agent_questions", _question, {"answer": "Acme"}),
        ("action", "operation_actions", _action, {"approved": True}),
    ],
)
def test_a_siblings_item_is_not_found_and_unchanged(child_admin, db_conn, kind, table, make, body):
    theirs = make(SIBLING)
    before = _status(db_conn, table, theirs)

    response = child_admin.post(f"/api/approvals/{kind}/{theirs}", json=body)

    assert response.status_code == 404
    assert _status(db_conn, table, theirs) == before


@pytest.mark.parametrize(
    ("kind", "table", "make", "body"),
    [
        ("workflow", "workflow_approvals", _approval, {"approved": True}),
        ("question", "agent_questions", _question, {"answer": "Acme"}),
        ("action", "operation_actions", _action, {"approved": True}),
    ],
)
def test_a_member_cannot_decide(child_member, db_conn, kind, table, make, body):
    mine = make(CHILD)
    before = _status(db_conn, table, mine)

    response = child_member.post(f"/api/approvals/{kind}/{mine}", json=body)

    assert response.status_code == 403
    assert _status(db_conn, table, mine) == before

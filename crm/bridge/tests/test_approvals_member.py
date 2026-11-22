"""The Inbox's member path: a non-operator lists and decides their OWN actions.

The rule itself (``robothor.rooms.approvals``) is proved against real rows in
``robothor/rooms/tests/test_member_approval.py``, as a full matrix. This file
proves the route around it:

* an operator's listing and decisions are exactly what they were;
* a non-operator's listing is ``{count, pending, scope: "own"}`` built only from
  ``list_member_actions`` (no workflow approval, no question, no tenant ledger
  read), each action row carrying ``room_id`` and ``task_id``;
* a non-operator may decide only ``kind=action``, only through
  ``decide_member_action``, recorded as ``member:<id>`` and audited;
* a refusal, or any failure of the check, is a 403 that settles nothing.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHILD = "acme-child-a"
BOB = str(uuid.uuid4())
ACTION_ID = str(uuid.uuid4())
ROOM_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())


def _client(role: str, actor: str, *, tenant: str = CHILD):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app)
    client.headers.update(
        {"Authorization": f"Bearer {tokens.issue_access_token(actor, tenant, role)}"}
    )
    return client


def _soon(**kw):
    return datetime.now(UTC) + timedelta(**kw)


def _member_row():
    return {
        "id": ACTION_ID,
        "kind": "sales.email",
        "payload": {
            "subject": "Following up",
            "recipient": "alice@example.com",
            "body": "Hello Alice",
        },
        "created_at": datetime.now(UTC),
        "expires_at": _soon(hours=20),
        "room_id": ROOM_ID,
        "task_id": TASK_ID,
    }


class _Ledger:
    """The tenant ledger an operator reads; a member must never construct it."""

    built: list[str] = []
    decisions: list[tuple[str, bool, str]] = []

    def __init__(self, tenant_id: str):
        _Ledger.built.append(tenant_id)

    def list_review(self, *, limit: int = 100):
        return [{**_member_row(), "id": str(uuid.uuid4())}]

    def get_action(self, action_id):
        return None

    def decide(self, action, approved, actor):
        _Ledger.decisions.append((action, approved, actor))


@pytest.fixture
def stores(monkeypatch):
    from routers import approvals

    _Ledger.built, _Ledger.decisions = [], []
    asked: dict[str, int] = {"workflow": 0, "question": 0}

    def _workflows(**kw):
        asked["workflow"] += 1
        return [
            SimpleNamespace(
                id=str(uuid.uuid4()),
                run_id=str(uuid.uuid4()),
                prompt="Send?",
                detail="",
                expires_at=_soon(hours=1),
                created_at=datetime.now(UTC),
            )
        ]

    def _questions(**kw):
        asked["question"] += 1
        return []

    monkeypatch.setattr(approvals, "list_pending_approvals", _workflows)
    monkeypatch.setattr(approvals, "list_pending_questions", _questions)
    monkeypatch.setattr(approvals, "Operations", _Ledger)
    return asked


@pytest.fixture
def member_rule(monkeypatch):
    """Stand-ins for the rule, recording the actor each was asked about."""
    from routers import approvals

    seen: dict[str, list] = {"list": [], "decide": []}
    state = {"rows": [_member_row()], "decide": True}

    def _list(actor):
        seen["list"].append(actor)
        return list(state["rows"])

    def _decide(actor, action_id, *, approved):
        seen["decide"].append((actor, action_id, approved))
        outcome = state["decide"]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(approvals, "list_member_actions", _list)
    monkeypatch.setattr(approvals, "decide_member_action", _decide)
    seen["state"] = state  # type: ignore[assignment]
    return seen


# ─── operators: unchanged ────────────────────────────────────────────────


def test_an_operator_listing_is_exactly_what_it_was(_controls_auth_key, stores, member_rule):
    body = _client("admin", "alice").get("/api/approvals").json()

    assert set(body) == {"count", "pending"}
    assert body["count"] == len(body["pending"]) == 2
    action = next(i for i in body["pending"] if i["kind"] == "action")
    assert set(action) == {
        "kind", "id", "run_id", "agent_id", "action_kind", "question", "detail", "envelope",
        "options", "expires_at", "created_at",
    }  # fmt: skip
    assert member_rule["list"] == []


def test_an_operator_decision_goes_to_the_ledger_as_before(_controls_auth_key, stores, member_rule):
    with patch("routers._audit.log_event"):
        response = _client("owner", "alice").post(
            f"/api/approvals/action/{ACTION_ID}", json={"approved": True}
        )

    assert response.json() == {
        "settled": True,
        "kind": "action",
        "id": ACTION_ID,
        "decision": "approved",
    }
    assert _Ledger.decisions == [(ACTION_ID, True, "operator:alice")]
    assert member_rule["decide"] == []


# ─── members: their own, nothing else ────────────────────────────────────


def test_a_viewer_is_still_refused_the_listing(_controls_auth_key, stores, member_rule):
    """A viewer can never post in a workspace, so nothing could ever be theirs to decide."""
    assert _client("viewer", BOB).get("/api/approvals").status_code == 403
    assert member_rule["list"] == []
    assert stores == {"workflow": 0, "question": 0}


@pytest.mark.parametrize("role", ["member", "user"])
def test_a_non_operator_lists_only_their_own_actions(_controls_auth_key, stores, member_rule, role):
    body = _client(role, BOB).get("/api/approvals").json()

    assert body["scope"] == "own"
    assert body["count"] == 1
    [item] = body["pending"]
    assert item["kind"] == "action" and item["id"] == ACTION_ID
    assert item["room_id"] == ROOM_ID and item["task_id"] == TASK_ID
    assert item["envelope"]["recipient"] == "alice@example.com"
    assert item["detail"].startswith("Hello Alice")
    assert stores == {"workflow": 0, "question": 0}
    assert _Ledger.built == []
    [who] = member_rule["list"]
    assert (who.user_id, who.role, who.tenant_id, who.is_service) == (BOB, role, CHILD, False)


def test_a_member_with_nothing_eligible_sees_an_empty_own_list(
    _controls_auth_key, stores, member_rule
):
    member_rule["state"]["rows"] = []

    assert _client("member", BOB).get("/api/approvals").json() == {
        "count": 0,
        "pending": [],
        "scope": "own",
    }


def test_a_failing_member_list_shows_nothing(_controls_auth_key, stores, monkeypatch):
    from routers import approvals

    def _boom(actor):
        raise RuntimeError("database down")

    monkeypatch.setattr(approvals, "list_member_actions", _boom)

    response = _client("member", BOB).get("/api/approvals")

    assert response.status_code == 503
    assert "pending" not in response.json()


def test_a_service_token_is_still_refused(_controls_auth_key, stores, member_rule):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {tokens.issue_service_token('main', CHILD)}"})

    assert client.get("/api/approvals").status_code in (401, 403)
    assert member_rule["list"] == []


def test_a_member_decides_their_own_action_as_a_member(_controls_auth_key, stores, member_rule):
    with patch("routers._audit.log_event") as log_event:
        response = _client("member", BOB).post(
            f"/api/approvals/action/{ACTION_ID}", json={"approved": False}
        )

    assert response.status_code == 200
    assert response.json() == {
        "settled": True,
        "kind": "action",
        "id": ACTION_ID,
        "decision": "rejected",
    }
    [(who, action_id, approved)] = member_rule["decide"]
    assert (who.user_id, who.role, action_id, approved) == (BOB, "member", ACTION_ID, False)
    details = log_event.call_args.kwargs["details"]
    assert details["decided_by"] == f"member:{BOB}"
    assert details["tenant_id"] == CHILD
    assert _Ledger.decisions == []


def test_a_member_refused_by_the_rule_gets_a_403_that_is_audited(
    _controls_auth_key, stores, member_rule
):
    from robothor.operations.store import NotPermitted

    member_rule["state"]["decide"] = NotPermitted("not yours")
    with patch("routers._audit.log_event") as log_event:
        response = _client("member", BOB).post(
            f"/api/approvals/action/{ACTION_ID}", json={"approved": True}
        )

    assert response.status_code == 403
    assert response.json()["settled"] is False
    assert log_event.call_args.kwargs["status"] == "denied"


def test_a_member_check_that_fails_is_a_403(_controls_auth_key, stores, member_rule):
    member_rule["state"]["decide"] = RuntimeError("database down")
    with patch("routers._audit.log_event"):
        response = _client("member", BOB).post(
            f"/api/approvals/action/{ACTION_ID}", json={"approved": True}
        )

    assert response.status_code == 403


def test_an_already_decided_member_action_settles_nothing(_controls_auth_key, stores, member_rule):
    from robothor.operations.store import Conflict

    member_rule["state"]["decide"] = Conflict("Draft is missing, expired, or already decided")
    with patch("routers._audit.log_event"):
        response = _client("member", BOB).post(
            f"/api/approvals/action/{ACTION_ID}", json={"approved": True}
        )

    assert response.status_code == 200
    assert response.json()["settled"] is False


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("workflow", {"approved": True}),
        ("question", {"answer": "Acme"}),
        ("escalation", {"approved": True}),
    ],
)
def test_a_member_still_cannot_decide_any_other_kind(
    _controls_auth_key, stores, member_rule, kind, body
):
    item = "a1b2c3d4e5f6" if kind == "escalation" else str(uuid.uuid4())
    with (
        patch("routers.approvals.decide_approval_by_id") as decide,
        patch("routers.approvals.answer_question") as answer,
        patch("routers.approvals.engine_request") as engine,
    ):
        response = _client("member", BOB).post(f"/api/approvals/{kind}/{item}", json=body)

    assert response.status_code == 403
    decide.assert_not_called()
    answer.assert_not_called()
    engine.assert_not_called()
    assert member_rule["decide"] == []


@pytest.mark.parametrize(
    ("item", "body", "code"),
    [
        ("not-a-uuid", {"approved": True}, 422),
        (ACTION_ID, {}, 400),
        (ACTION_ID, {"approved": "yes please"}, 400),
    ],
)
def test_a_malformed_member_decision_settles_nothing(
    _controls_auth_key, stores, member_rule, item, body, code
):
    with patch("routers._audit.log_event"):
        response = _client("member", BOB).post(f"/api/approvals/action/{item}", json=body)

    assert response.status_code == code
    assert member_rule["decide"] == []


def test_the_member_path_runs_inside_the_acting_tenants_scope(_controls_auth_key, monkeypatch):
    from routers import approvals

    from robothor.db.connection import current_tenant_scope

    scopes: list = []

    def _list(actor):
        scopes.append(("list", current_tenant_scope()))
        return []

    def _decide(actor, action_id, *, approved):
        scopes.append(("decide", current_tenant_scope()))
        return True

    monkeypatch.setattr(approvals, "list_member_actions", _list)
    monkeypatch.setattr(approvals, "decide_member_action", _decide)
    client = _client("member", BOB)
    client.get("/api/approvals")
    with patch("routers._audit.log_event"):
        client.post(f"/api/approvals/action/{ACTION_ID}", json={"approved": True})

    assert scopes == [("list", CHILD), ("decide", CHILD)]


def test_a_workspace_email_card_names_its_sender_recipients_and_body(
    _controls_auth_key, stores, member_rule
):
    payload = {
        "version": 1,
        "sender": "assistant@example.com",
        "reply_to": "bob@example.com",
        "recipient": ["carol.client@example.org", "dave.client@example.org"],
        "cc": ["erin.client@example.org"],
        "subject": "Renewal follow-up",
        "body": "Hello,\nShall we renew?",
        "on_behalf_of": {"user_id": BOB, "display_name": "Bob"},
        "room_id": ROOM_ID,
        "task_id": TASK_ID,
    }
    member_rule["state"]["rows"] = [{**_member_row(), "kind": "room.email", "payload": payload}]

    [item] = _client("member", BOB).get("/api/approvals").json()["pending"]

    assert item["action_kind"] == "room.email"
    assert item["envelope"] == {
        "sender": "assistant@example.com",
        "recipient": "carol.client@example.org, dave.client@example.org",
        "subject": "Renewal follow-up",
    }
    assert item["detail"].startswith("Hello,\nShall we renew?")
    for shown in ("erin.client@example.org", "bob@example.com", "reply_to"):
        assert shown in item["detail"]


def test_a_workspace_email_row_carries_the_exact_email(_controls_auth_key, stores, member_rule):
    """The task workspace's card shows the email field by field, exactly as it
    would be sent: from, reply-to, to, cc, subject and the whole body."""
    payload = {
        "version": 1,
        "sender": "assistant@example.com",
        "reply_to": "bob@example.com",
        "recipient": ["carol.client@example.org", "dave.client@example.org"],
        "cc": ["erin.client@example.org"],
        "subject": "Renewal follow-up",
        "body": "Hello,\nShall we renew?\n\n" + "More. " * 400,
        "on_behalf_of": {"user_id": BOB, "display_name": "Bob"},
        "room_id": ROOM_ID,
        "task_id": TASK_ID,
    }
    member_rule["state"]["rows"] = [{**_member_row(), "kind": "room.email", "payload": payload}]

    [item] = _client("member", BOB).get("/api/approvals").json()["pending"]

    assert item["email"] == {
        "from": "assistant@example.com",
        "replyTo": "bob@example.com",
        "to": ["carol.client@example.org", "dave.client@example.org"],
        "cc": ["erin.client@example.org"],
        "subject": "Renewal follow-up",
        "body": payload["body"],
        "onBehalfOf": "Bob",
    }


def test_no_other_kind_carries_an_email_field(_controls_auth_key, stores, member_rule):
    member_rule["state"]["rows"] = [_member_row()]
    [item] = _client("member", BOB).get("/api/approvals").json()["pending"]
    assert "email" not in item

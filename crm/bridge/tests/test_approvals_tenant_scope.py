"""The Inbox is tenant-local: an admin of a child tenant decides that tenant's items.

``test_approvals_router.py`` pins the platform operator's behaviour. This file
pins what changes for an owner or admin of any OTHER tenant:

* the listing names only the acting tenant's workflow approvals, agent
  questions and proposed actions (``operation_actions`` in review);
* every store call runs inside ``tenant_scope(<acting tenant>)``, so row-level
  security binds the same tenant the SQL filters on;
* a decision carries ``operator:<user>`` and is audited with the tenant;
* another tenant's id is a 404 and settles nothing;
* a member or viewer is still a 403;
* an engine escalation stays platform-only, because the engine process that
  holds it is not tenant-scoped.

Generic fixtures only: ``acme-child-a`` acts, ``acme-child-b`` is the neighbour.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHILD = "acme-child-a"
APPROVAL_ID = str(uuid.uuid4())
QUESTION_ID = str(uuid.uuid4())
ACTION_ID = str(uuid.uuid4())


def _client(role: str, tenant: str, actor: str = "alice"):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    token = tokens.issue_access_token(actor, tenant, role)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture
def child_admin(_controls_auth_key):
    return _client("admin", CHILD)


@pytest.fixture
def child_member(_controls_auth_key):
    return _client("member", CHILD, actor="bob")


@pytest.fixture
def child_viewer(_controls_auth_key):
    return _client("viewer", CHILD, actor="bob")


def _scope() -> str | None:
    from robothor.db.connection import current_tenant_scope

    return current_tenant_scope()


def _soon(**kw):
    return datetime.now(UTC) + timedelta(**kw)


class FakeOperations:
    """Stands in for ``robothor.operations.store.Operations`` in the router."""

    instances: list[FakeOperations] = []
    review: list[dict] = []
    known: set[str] = set()
    decide_error: Exception | None = None

    def __init__(self, tenant_id: str):
        self.tenant = tenant_id
        self.decisions: list[tuple[str, bool, str, str | None]] = []
        FakeOperations.instances.append(self)

    def list_review(self, *, limit: int = 100) -> list[dict]:
        self.list_scope = _scope()
        return list(FakeOperations.review)

    def get_action(self, action_id: str) -> dict | None:
        return {"id": action_id, "status": "approved"} if action_id in self.known else None

    def decide(self, action: str, approved: bool, actor: str) -> None:
        self.decisions.append((action, approved, actor, _scope()))
        if FakeOperations.decide_error is not None:
            raise FakeOperations.decide_error


@pytest.fixture
def ops(monkeypatch):
    from routers import approvals

    FakeOperations.instances = []
    FakeOperations.review = [
        {
            "id": ACTION_ID,
            "kind": "sales.email",
            "payload": {"subject": "Intro to Acme"},
            "expires_at": _soon(hours=20),
            "created_at": datetime.now(UTC),
        }
    ]
    FakeOperations.known = set()
    FakeOperations.decide_error = None
    monkeypatch.setattr(approvals, "Operations", FakeOperations)
    return FakeOperations


@pytest.fixture
def stores(monkeypatch, ops):
    """Listing stubs that record the tenant they were asked for, and the RLS scope."""
    from routers import approvals

    calls: dict[str, tuple] = {}

    def _approvals(**kw):
        calls["approvals"] = (kw.get("tenant_id"), _scope())
        return [
            SimpleNamespace(
                id=APPROVAL_ID,
                run_id=str(uuid.uuid4()),
                prompt="Send the report?",
                detail="",
                expires_at=_soon(hours=2),
                created_at=datetime.now(UTC),
            )
        ]

    def _questions(**kw):
        calls["questions"] = (kw.get("tenant_id"), _scope())
        return [
            SimpleNamespace(
                id=QUESTION_ID,
                run_id=str(uuid.uuid4()),
                agent_id="assistant",
                question="Which vendor?",
                options=["Acme", "Globex"],
                expires_at=_soon(minutes=5),
                created_at=datetime.now(UTC),
            )
        ]

    monkeypatch.setattr(approvals, "list_pending_approvals", _approvals)
    monkeypatch.setattr(approvals, "list_pending_questions", _questions)
    return calls


# ─── Listing ────────────────────────────────────────────────────────


def test_a_child_admin_lists_their_own_tenant(child_admin, stores, ops):
    response = child_admin.get("/api/approvals")

    assert response.status_code == 200
    assert stores["approvals"] == (CHILD, CHILD)
    assert stores["questions"] == (CHILD, CHILD)
    assert [o.tenant for o in ops.instances] == [CHILD]
    assert ops.instances[0].list_scope == CHILD


def test_the_listing_includes_proposed_actions_in_review(child_admin, stores, ops):
    body = child_admin.get("/api/approvals").json()

    by_kind = {item["kind"]: item for item in body["pending"]}
    assert set(by_kind) == {"workflow", "question", "action"}
    assert body["count"] == 3
    action = by_kind["action"]
    assert action["id"] == ACTION_ID
    assert action["action_kind"] == "sales.email"
    assert action["question"]
    assert action["options"] == []
    assert action["expires_at"]


def _email_proposal(body: str) -> dict:
    """The shape ``Sales.draft`` proposes: body first when keys are sorted."""
    return {
        "id": ACTION_ID,
        "kind": "sales.email",
        "payload": {
            "body": body,
            "claim_ids": ["claim-1"],
            "contact_identity_hash": "h",
            "evidence_ids": ["ev-1", "ev-2"],
            "purpose": "introduction",
            "recipient": "bob@example.com",
            "sender": "agent@example.com",
            "subject": "Intro to Acme",
        },
        "expires_at": _soon(hours=20),
        "created_at": datetime.now(UTC),
    }


def test_a_proposed_email_shows_who_it_goes_to_and_its_whole_body(child_admin, stores, ops):
    """Approve authorizes the exact payload, so the card must be able to show it."""
    body_text = "Hello Bob,\n\n" + "A long paragraph about the proposal. " * 120
    ops.review = [_email_proposal(body_text)]

    listed = child_admin.get("/api/approvals").json()["pending"]
    [action] = [item for item in listed if item["kind"] == "action"]

    assert action["envelope"] == {
        "sender": "agent@example.com",
        "recipient": "bob@example.com",
        "purpose": "introduction",
        "subject": "Intro to Acme",
        "claims": "claim-1",
        "evidence": "ev-1, ev-2",
    }
    assert action["detail"] == body_text


def test_a_proposal_with_a_body_shows_every_other_field_too(child_admin, stores, ops):
    """A plugin's payload may address people in keys the envelope does not name;
    Approve authorizes all of it, so the card shows the body and the rest."""
    payload = {
        "to": "bob@example.com",
        "cc": ["carol@example.com"],
        "body": "Hello Bob",
        "urgent": True,
    }
    ops.review = [{**_email_proposal(""), "kind": "acme-plugin.email", "payload": payload}]

    [action] = [
        i for i in child_admin.get("/api/approvals").json()["pending"] if i["kind"] == "action"
    ]

    assert action["envelope"] == {}
    assert action["detail"].startswith("Hello Bob")
    rest = json.loads(action["detail"][len("Hello Bob") :])
    assert rest == {"to": "bob@example.com", "cc": ["carol@example.com"], "urgent": True}


def test_an_envelope_key_the_envelope_cannot_show_is_printed_with_the_rest(child_admin, stores, ops):
    """A recipient given as an object is not an envelope string, so it goes in the detail."""
    payload = {"recipient": {"email": "bob@example.com", "name": "Bob"}, "body": "Hello Bob"}
    ops.review = [{**_email_proposal(""), "kind": "acme-plugin.email", "payload": payload}]

    [action] = [
        i for i in child_admin.get("/api/approvals").json()["pending"] if i["kind"] == "action"
    ]

    assert action["envelope"] == {}
    rest = json.loads(action["detail"][len("Hello Bob") :])
    assert rest == {"recipient": {"email": "bob@example.com", "name": "Bob"}}


def test_a_proposal_without_a_body_shows_its_whole_payload(child_admin, stores, ops):
    big = {"items": ["x" * 50] * 40}
    ops.review = [{**_email_proposal(""), "kind": "crm.update", "payload": big}]

    [action] = [
        i for i in child_admin.get("/api/approvals").json()["pending"] if i["kind"] == "action"
    ]

    assert action["envelope"] == {}
    assert json.loads(action["detail"]) == big


@pytest.mark.parametrize("fixture", ["child_member", "child_viewer"])
def test_members_and_viewers_cannot_list(fixture, request, stores):
    client = request.getfixturevalue(fixture)
    assert client.get("/api/approvals").status_code == 403
    assert stores == {}


# ─── Deciding ───────────────────────────────────────────────────────


def test_a_child_admin_decides_their_workflow_approval_in_their_tenant(child_admin):
    seen = {}

    def _decide(approval_id, decision, **kw):
        seen.update(kw, id=approval_id, scope=_scope())
        return True

    with (
        patch("routers.approvals.decide_approval_by_id", _decide),
        patch("routers._audit.log_event") as log_event,
    ):
        response = child_admin.post(
            f"/api/approvals/workflow/{APPROVAL_ID}", json={"approved": True}
        )

    assert response.status_code == 200
    assert response.json()["settled"] is True
    assert seen["tenant_id"] == CHILD
    assert seen["scope"] == CHILD
    assert seen["decided_by"] == "operator:alice"
    details = log_event.call_args.kwargs["details"]
    assert details["tenant_id"] == CHILD
    assert details["decided_by"] == "operator:alice"


def test_a_child_admin_answers_their_question_in_their_tenant(child_admin):
    seen = {}

    def _answer(question_id, answer, **kw):
        seen.update(kw, scope=_scope())
        return True

    with patch("routers.approvals.answer_question", _answer), patch("routers._audit.log_event"):
        response = child_admin.post(
            f"/api/approvals/question/{QUESTION_ID}", json={"answer": "Acme"}
        )

    assert response.status_code == 200
    assert seen == {"answered_by": "operator:alice", "tenant_id": CHILD, "scope": CHILD}


@pytest.mark.parametrize("approved", [True, False])
def test_a_child_admin_decides_a_proposed_action(child_admin, ops, approved):
    with patch("routers._audit.log_event") as log_event:
        response = child_admin.post(
            f"/api/approvals/action/{ACTION_ID}", json={"approved": approved}
        )

    assert response.status_code == 200
    assert response.json()["settled"] is True
    [instance] = ops.instances
    assert instance.tenant == CHILD
    assert instance.decisions == [(ACTION_ID, approved, "operator:alice", CHILD)]
    details = log_event.call_args.kwargs["details"]
    assert details["kind"] == "action"
    assert details["tenant_id"] == CHILD
    assert details["decided_by"] == "operator:alice"


def test_an_action_needs_a_verdict(child_admin, ops):
    with patch("routers._audit.log_event"):
        response = child_admin.post(f"/api/approvals/action/{ACTION_ID}", json={})
    assert response.status_code == 400
    assert ops.instances == [] or ops.instances[0].decisions == []


def test_a_non_uuid_action_id_is_refused(child_admin, ops):
    with patch("routers._audit.log_event"):
        response = child_admin.post("/api/approvals/action/not-a-uuid", json={"approved": True})
    assert response.status_code == 422


def test_an_action_already_decided_is_reported_not_faked(child_admin, ops):
    from robothor.operations.store import Conflict

    ops.decide_error = Conflict("Draft is missing, expired, or already decided")
    ops.known = {ACTION_ID}
    with patch("routers._audit.log_event"):
        response = child_admin.post(f"/api/approvals/action/{ACTION_ID}", json={"approved": True})
    assert response.status_code == 200
    assert response.json()["settled"] is False


# ─── Another tenant's id ────────────────────────────────────────────


def test_another_tenants_workflow_approval_is_not_found(child_admin):
    with (
        patch("routers.approvals.decide_approval_by_id", return_value=False),
        patch("routers.approvals.get_approval_by_id", return_value=None) as lookup,
        patch("routers._audit.log_event") as log_event,
    ):
        response = child_admin.post(
            f"/api/approvals/workflow/{APPROVAL_ID}", json={"approved": True}
        )

    assert response.status_code == 404
    assert lookup.call_args.kwargs["tenant_id"] == CHILD
    assert log_event.call_args.kwargs["status"] == "denied"


def test_another_tenants_question_is_not_found(child_admin):
    with (
        patch("routers.approvals.answer_question", return_value=False),
        patch("routers.approvals.get_question", return_value=None) as lookup,
        patch("routers._audit.log_event"),
    ):
        response = child_admin.post(f"/api/approvals/question/{QUESTION_ID}", json={"answer": "x"})

    assert response.status_code == 404
    assert lookup.call_args.kwargs["tenant_id"] == CHILD


def test_another_tenants_action_is_not_found(child_admin, ops):
    from robothor.operations.store import Conflict

    ops.decide_error = Conflict("Draft is missing, expired, or already decided")
    with patch("routers._audit.log_event"):
        response = child_admin.post(f"/api/approvals/action/{ACTION_ID}", json={"approved": True})
    assert response.status_code == 404


# ─── Refusals ───────────────────────────────────────────────────────


@pytest.mark.parametrize("fixture", ["child_member", "child_viewer"])
@pytest.mark.parametrize(
    ("kind", "item_id", "body"),
    [
        ("workflow", APPROVAL_ID, {"approved": True}),
        ("question", QUESTION_ID, {"answer": "Acme"}),
        ("action", ACTION_ID, {"approved": True}),
    ],
    ids=["workflow", "question", "action"],
)
def test_members_and_viewers_cannot_decide(fixture, kind, item_id, body, request, ops):
    client = request.getfixturevalue(fixture)
    with (
        patch("routers.approvals.decide_approval_by_id") as decide,
        patch("routers.approvals.answer_question") as answer,
    ):
        response = client.post(f"/api/approvals/{kind}/{item_id}", json=body)

    assert response.status_code == 403
    decide.assert_not_called()
    answer.assert_not_called()
    assert ops.instances == []


def test_an_engine_escalation_stays_platform_only(child_admin):
    engine = AsyncMock(return_value=(200, {"resolved": True}))
    with patch("routers.approvals.engine_request", engine), patch("routers._audit.log_event"):
        response = child_admin.post(
            "/api/approvals/escalation/a1b2c3d4e5f6", json={"approved": True}
        )

    assert response.status_code == 403
    engine.assert_not_awaited()

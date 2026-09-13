"""The Helm's answer surface: what is waiting on a person, and answering it.

Three kinds of question end up here and they are NOT interchangeable, which is
the whole reason this router exists rather than one more endpoint bolted onto an
existing one:

* ``workflow`` — a durable ``workflow_approvals`` row. A verdict.
* ``question`` — a durable ``agent_questions`` row. Free text.
* ``escalation`` — an ``asyncio.Event`` inside the ENGINE process. Writing a row
  for one of these would settle nothing at all, so it is proxied.

Every route is operator-gated and every successful answer is audited. The tests
below assert both, plus the one property that is easy to lose in a refactor: an
escalation must never be answered by touching the database.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

QUESTION_ID = str(uuid.uuid4())
APPROVAL_ID = str(uuid.uuid4())
ESCALATION_ID = "a1b2c3d4e5f6"


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _pending_question():
    from datetime import UTC, datetime, timedelta

    return _Row(
        id=QUESTION_ID,
        run_id=str(uuid.uuid4()),
        agent_id="assistant",
        kind="question",
        question="Which vendor?",
        options=["Acme", "Globex"],
        channel="telegram",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        created_at=datetime.now(UTC),
    )


def _pending_workflow_approval():
    from datetime import UTC, datetime, timedelta

    return _Row(
        id=APPROVAL_ID,
        run_id=str(uuid.uuid4()),
        workflow_id="quarterly-report",
        step_id="confirm-send",
        prompt="Send the report?",
        detail="4 recipients",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def stores(monkeypatch):
    from routers import approvals

    monkeypatch.setattr(
        approvals, "list_pending_approvals", lambda **kw: [_pending_workflow_approval()]
    )
    monkeypatch.setattr(approvals, "list_pending_questions", lambda **kw: [_pending_question()])
    return approvals


# ─── The gate ───────────────────────────────────────────────────────


def test_listing_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/approvals").status_code == 403


def test_answering_requires_operator(controls_client_as_viewer):
    response = controls_client_as_viewer.post(
        f"/api/approvals/question/{QUESTION_ID}", json={"answer": "Acme"}
    )
    assert response.status_code == 403


def test_a_refused_caller_settles_nothing(controls_client_as_viewer):
    with patch("routers.approvals.answer_question") as answer:
        controls_client_as_viewer.post(
            f"/api/approvals/question/{QUESTION_ID}", json={"answer": "Acme"}
        )
    answer.assert_not_called()


# ─── Listing ────────────────────────────────────────────────────────


def test_listing_composes_both_kinds(controls_client_as_operator, stores):
    body = controls_client_as_operator.get("/api/approvals").json()

    kinds = {item["kind"] for item in body["pending"]}
    assert kinds == {"workflow", "question"}
    assert body["count"] == 2


def test_listing_names_the_id_the_answer_endpoint_takes(controls_client_as_operator, stores):
    body = controls_client_as_operator.get("/api/approvals").json()

    by_kind = {item["kind"]: item for item in body["pending"]}
    assert by_kind["question"]["id"] == QUESTION_ID
    assert by_kind["workflow"]["id"] == APPROVAL_ID
    assert by_kind["question"]["options"] == ["Acme", "Globex"]


# ─── Answering a durable question ───────────────────────────────────


def test_answering_a_question_writes_the_answer_and_audits_it(controls_client_as_operator):
    with (
        patch("routers.approvals.answer_question", return_value=True) as answer,
        patch("routers._audit.log_event") as log_event,
    ):
        response = controls_client_as_operator.post(
            f"/api/approvals/question/{QUESTION_ID}", json={"answer": "Acme"}
        )

    assert response.status_code == 200
    assert response.json()["settled"] is True
    assert answer.call_args.args[0] == QUESTION_ID
    assert answer.call_args.args[1] == "Acme"

    assert log_event.call_count == 1
    assert log_event.call_args.args[0] == "approval.answer"
    assert log_event.call_args.kwargs["action"] == QUESTION_ID
    assert log_event.call_args.kwargs["status"] == "ok"
    # Identifiers only — the answer text is content, not an identifier.
    assert "Acme" not in repr(log_event.call_args.kwargs["details"])


def test_a_question_already_answered_is_reported_not_faked(controls_client_as_operator):
    with (
        patch("routers.approvals.answer_question", return_value=False),
        patch("routers._audit.log_event") as log_event,
    ):
        body = controls_client_as_operator.post(
            f"/api/approvals/question/{QUESTION_ID}", json={"answer": "Acme"}
        ).json()

    assert body["settled"] is False
    assert log_event.call_args.kwargs["status"] == "error"


def test_a_question_needs_an_answer(controls_client_as_operator):
    response = controls_client_as_operator.post(f"/api/approvals/question/{QUESTION_ID}", json={})
    assert response.status_code == 400


# ─── Answering a workflow step ──────────────────────────────────────


def test_approving_a_workflow_step_decides_the_row(controls_client_as_operator):
    with (
        patch("routers.approvals.decide_approval_by_id", return_value=True) as decide,
        patch("routers._audit.log_event"),
    ):
        body = controls_client_as_operator.post(
            f"/api/approvals/workflow/{APPROVAL_ID}", json={"approved": True}
        ).json()

    assert body["settled"] is True
    assert decide.call_args.args[0] == APPROVAL_ID
    assert decide.call_args.args[1].value == "approved"


def test_rejecting_a_workflow_step_decides_the_row(controls_client_as_operator):
    with (
        patch("routers.approvals.decide_approval_by_id", return_value=True) as decide,
        patch("routers._audit.log_event"),
    ):
        controls_client_as_operator.post(
            f"/api/approvals/workflow/{APPROVAL_ID}", json={"approved": False}
        )

    assert decide.call_args.args[1].value == "rejected"


# ─── Answering an in-RAM escalation ─────────────────────────────────


def test_an_escalation_is_proxied_to_the_engine_and_never_written(controls_client_as_operator):
    """The pending request is an asyncio.Event in another process. A row would
    settle nothing and the agent would wait until its timeout denied it."""
    engine = AsyncMock(return_value=(200, {"settled": True, "approved": True}))
    with (
        patch("routers.approvals.engine_request", engine),
        patch("routers.approvals.answer_question") as answer,
        patch("routers.approvals.decide_approval_by_id") as decide,
        patch("routers._audit.log_event"),
    ):
        body = controls_client_as_operator.post(
            f"/api/approvals/escalation/{ESCALATION_ID}", json={"approved": True}
        ).json()

    assert body["settled"] is True
    answer.assert_not_called()
    decide.assert_not_called()

    method, path = engine.call_args.args
    assert method == "POST"
    assert path == f"/api/admin/approvals/escalation/{ESCALATION_ID}"
    assert engine.call_args.kwargs["json"] == {"approved": True, "remember_session": False}


def test_an_escalation_the_engine_no_longer_holds_is_reported(controls_client_as_operator):
    engine = AsyncMock(return_value=(404, {"settled": False}))
    with (
        patch("routers.approvals.engine_request", engine),
        patch("routers._audit.log_event") as log_event,
    ):
        response = controls_client_as_operator.post(
            f"/api/approvals/escalation/{ESCALATION_ID}", json={"approved": True}
        )

    assert response.status_code == 404
    assert log_event.call_args.kwargs["status"] == "error"


def test_an_escalation_id_that_is_not_an_identifier_is_refused_before_the_proxy(
    controls_client_as_operator,
):
    """``engine_request`` builds a URL from this. A path segment is the one
    place a caller-supplied string turns into somewhere else's server."""
    engine = AsyncMock()
    with patch("routers.approvals.engine_request", engine), patch("routers._audit.log_event"):
        response = controls_client_as_operator.post(
            "/api/approvals/escalation/..%2F..%2Fetc", json={"approved": True}
        )

    assert response.status_code in (400, 404)
    engine.assert_not_called()


# ─── Shape ──────────────────────────────────────────────────────────


def test_an_unknown_kind_is_refused(controls_client_as_operator):
    response = controls_client_as_operator.post(
        f"/api/approvals/nonsense/{QUESTION_ID}", json={"approved": True}
    )
    assert response.status_code in (400, 404, 422)

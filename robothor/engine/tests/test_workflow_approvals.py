"""Durable workflow approvals — integration tests against a real database.

The whole point of this feature is that a pending question survives things
that kill a process, so tests that mock the store would certify nothing. Each
test here writes real rows and reads them back through a fresh call path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from robothor.engine.approvals import (
    ApprovalDecision,
    decide_approval,
    expire_overdue_approvals,
    get_approval,
    list_decided_approvals,
    list_pending_approvals,
    request_approval,
)

pytestmark = pytest.mark.integration

TENANT = "robothor-primary"


@pytest.fixture
def run_id() -> str:
    return str(uuid.uuid4())


def _cleanup(run_id: str) -> None:
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_approvals WHERE run_id = %s", (run_id,))
        conn.commit()


class TestRequestAndRead:
    def test_a_request_is_a_row_that_outlives_the_caller(self, run_id):
        try:
            req = request_approval(
                run_id=run_id,
                workflow_id="quarterly-report",
                step_id="confirm-send",
                prompt="Send the Q3 report to the board?",
                detail="Recipients: 4. Attachment: q3.pdf (2.1 MB)",
                timeout_hours=24,
                tenant_id=TENANT,
            )
            assert req.status == "pending"

            # Read back through an independent call — nothing in-process is
            # keeping this alive.
            fetched = get_approval(run_id, "confirm-send", tenant_id=TENANT)
            assert fetched is not None
            assert fetched.prompt == "Send the Q3 report to the board?"
            assert fetched.detail.startswith("Recipients: 4")
            assert fetched.status == "pending"
        finally:
            _cleanup(run_id)

    def test_asking_twice_returns_the_same_question(self, run_id):
        """The resume interlock: a restarted run must not re-ask.

        Without this a crash loop pages the operator once per restart, which
        is how a genuinely useful prompt gets muted.
        """
        try:
            first = request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            second = request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed? (different wording, same step)",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            assert second.id == first.id
            assert second.prompt == "Proceed?"  # the ORIGINAL question stands
        finally:
            _cleanup(run_id)

    def test_a_decided_request_is_not_reopened_by_re_asking(self, run_id):
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            decide_approval(
                run_id, "gate", ApprovalDecision.APPROVED, decided_by="operator", tenant_id=TENANT
            )
            again = request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            assert again.status == "approved"
        finally:
            _cleanup(run_id)


class TestDecisions:
    def test_approve_records_who_and_when(self, run_id):
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            ok = decide_approval(
                run_id,
                "gate",
                ApprovalDecision.APPROVED,
                decided_by="operator",
                note="checked the numbers",
                tenant_id=TENANT,
            )
            assert ok is True

            row = get_approval(run_id, "gate", tenant_id=TENANT)
            assert row.status == "approved"
            assert row.decided_by == "operator"
            assert row.decision_note == "checked the numbers"
            assert row.decided_at is not None
        finally:
            _cleanup(run_id)

    def test_reject_is_recorded_as_a_decision_not_an_absence(self, run_id):
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            decide_approval(
                run_id, "gate", ApprovalDecision.REJECTED, decided_by="operator", tenant_id=TENANT
            )
            row = get_approval(run_id, "gate", tenant_id=TENANT)
            assert row.status == "rejected"
            assert row.decided_at is not None
        finally:
            _cleanup(run_id)

    def test_the_first_decision_wins(self, run_id):
        """Two operators, or a retry, must not flip a settled decision."""
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            assert decide_approval(
                run_id, "gate", ApprovalDecision.APPROVED, decided_by="alice", tenant_id=TENANT
            )
            assert not decide_approval(
                run_id, "gate", ApprovalDecision.REJECTED, decided_by="bob", tenant_id=TENANT
            )

            row = get_approval(run_id, "gate", tenant_id=TENANT)
            assert row.status == "approved"
            assert row.decided_by == "alice"
        finally:
            _cleanup(run_id)

    def test_deciding_an_unknown_request_is_false_not_an_exception(self, run_id):
        assert not decide_approval(
            run_id, "nope", ApprovalDecision.APPROVED, decided_by="operator", tenant_id=TENANT
        )


class TestExpiry:
    def test_an_overdue_request_is_stamped_expired_and_kept(self, run_id):
        """Timeout is a recorded outcome, not a deletion.

        'Nobody answered' is exactly the fact an operator needs after the
        fact, and it is the one a cleanup DELETE would destroy.
        """
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            _force_expiry(run_id, "gate")

            expired = expire_overdue_approvals(tenant_id=TENANT)
            assert any(a.run_id == run_id for a in expired)

            row = get_approval(run_id, "gate", tenant_id=TENANT)
            assert row.status == "expired"
            assert row.decided_at is not None
        finally:
            _cleanup(run_id)

    def test_expiry_never_touches_a_decided_request(self, run_id):
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            decide_approval(
                run_id, "gate", ApprovalDecision.APPROVED, decided_by="operator", tenant_id=TENANT
            )
            _force_expiry(run_id, "gate")

            expire_overdue_approvals(tenant_id=TENANT)
            assert get_approval(run_id, "gate", tenant_id=TENANT).status == "approved"
        finally:
            _cleanup(run_id)

    def test_expiry_is_idempotent(self, run_id):
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            _force_expiry(run_id, "gate")
            first = expire_overdue_approvals(tenant_id=TENANT)
            second = expire_overdue_approvals(tenant_id=TENANT)
            assert any(a.run_id == run_id for a in first)
            assert not any(a.run_id == run_id for a in second)
        finally:
            _cleanup(run_id)


class TestDriverQueries:
    def test_pending_list_finds_the_question(self, run_id):
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            assert any(a.run_id == run_id for a in list_pending_approvals(tenant_id=TENANT))
        finally:
            _cleanup(run_id)

    def test_decided_list_is_what_the_resume_driver_reads(self, run_id):
        try:
            request_approval(
                run_id=run_id,
                workflow_id="wf",
                step_id="gate",
                prompt="Proceed?",
                detail="",
                timeout_hours=1,
                tenant_id=TENANT,
            )
            assert not any(a.run_id == run_id for a in list_decided_approvals(tenant_id=TENANT))

            decide_approval(
                run_id, "gate", ApprovalDecision.APPROVED, decided_by="operator", tenant_id=TENANT
            )
            decided = list_decided_approvals(tenant_id=TENANT)
            assert any(a.run_id == run_id and a.status == "approved" for a in decided)
        finally:
            _cleanup(run_id)


def _force_expiry(run_id: str, step_id: str) -> None:
    """Backdate the deadline — the only way to test a 1h timeout in 1ms."""
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_approvals SET expires_at = %s WHERE run_id = %s AND step_id = %s",
            (datetime.now(UTC) - timedelta(minutes=1), run_id, step_id),
        )
        conn.commit()

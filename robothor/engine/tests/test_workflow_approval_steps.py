"""The approval STEP — suspend, survive, resume.

These run against a real database on purpose. The claim under test is "a
pending question survives the process", and every test here proves it the
only way that means anything: by throwing away the engine that asked and
finishing the run with a different one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.approvals import ApprovalDecision, decide_approval, get_approval
from robothor.engine.models import RunStatus, WorkflowStepStatus
from robothor.engine.workflow import WorkflowEngine, parse_workflow

pytestmark = pytest.mark.integration

TENANT = "robothor-primary"


def _wf(**step_overrides) -> dict:
    """A three-step workflow with an approval gate in the middle."""
    gate = {
        "id": "gate",
        "type": "approval",
        "prompt": "Send the report?",
        "approval_timeout_hours": 24,
    }
    gate.update(step_overrides)
    return {
        "id": f"approval-test-{uuid.uuid4().hex[:8]}",
        "name": "Approval test",
        "steps": [
            {"id": "prepare", "type": "transform", "expression": "ready"},
            gate,
            {"id": "send", "type": "transform", "expression": "sent"},
        ],
    }


def _engine(wf_data: dict) -> WorkflowEngine:
    """An engine holding exactly one workflow, with alerting stubbed.

    A fresh engine per call is the point: resuming through a *different*
    instance than the one that suspended is what proves nothing in-process is
    holding the run together.
    """
    config = MagicMock()
    config.tenant_id = TENANT
    engine = WorkflowEngine(config, runner=MagicMock())
    engine._workflows[wf_data["id"]] = parse_workflow(wf_data)
    return engine


def _cleanup(run_id: str) -> None:
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_approvals WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM workflow_run_steps WHERE run_id = %s", (run_id,))
        cur.execute("DELETE FROM workflow_runs WHERE id = %s", (run_id,))
        conn.commit()


@pytest.fixture
def no_alerts():
    """Silence the operator channels; their wiring is tested separately."""
    with (
        patch("robothor.engine.workflow.WorkflowEngine._notify_approval_request"),
        patch("robothor.engine.workflow.WorkflowEngine._notify_run_failure"),
    ):
        yield


async def _run(engine: WorkflowEngine, wf_id: str):
    return await engine.execute(wf_id, trigger_type="cron")


class TestSuspend:
    @pytest.mark.asyncio
    async def test_the_run_stops_at_the_gate_and_says_so(self, no_alerts):
        data = _wf()
        engine = _engine(data)
        run = await _run(engine, data["id"])
        try:
            assert run.status == RunStatus.AWAITING_APPROVAL
            # The step after the gate must NOT have run.
            assert "send" not in run.context["steps"]
            assert run.context["steps"]["prepare"]["status"] == "completed"
            assert run.context["_resume_step"] == "gate"
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_a_suspended_run_carries_no_completion_time(self, no_alerts):
        """It has not completed. Stamping a time would make every duration
        query downstream read the wait as work."""
        data = _wf()
        engine = _engine(data)
        run = await _run(engine, data["id"])
        try:
            assert run.completed_at is None

            from robothor.db.connection import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT status, completed_at FROM workflow_runs WHERE id = %s", (run.id,)
                )
                status, completed_at = cur.fetchone()
            assert status == "awaiting_approval"
            assert completed_at is None
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_the_question_is_a_row_with_the_rendered_prompt(self, no_alerts):
        data = _wf()
        engine = _engine(data)
        run = await _run(engine, data["id"])
        try:
            req = get_approval(run.id, "gate", tenant_id=TENANT)
            assert req is not None
            assert req.prompt == "Send the report?"
            assert req.status == "pending"
            assert run.id in req.detail  # the operator can find the run
        finally:
            _cleanup(run.id)


class TestResume:
    @pytest.mark.asyncio
    async def test_approval_finishes_the_run_in_a_brand_new_engine(self, no_alerts):
        """The durability claim, stated as a test.

        The engine that asked is discarded. A second engine — the stand-in
        for the process that comes back after a restart — reads the decision
        out of Postgres and finishes the work.
        """
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            assert run.status == RunStatus.AWAITING_APPROVAL

            decide_approval(
                run.id, "gate", ApprovalDecision.APPROVED, decided_by="operator", tenant_id=TENANT
            )

            resumed = await _engine(data).resume_run(run.id)
            assert resumed is not None
            assert resumed.status == RunStatus.COMPLETED
            assert resumed.context["steps"]["send"]["status"] == "completed"
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_rejection_stops_the_run_without_calling_it_a_failure(self, no_alerts):
        """A declined action is the control working, not an error.

        FAILED would page the operator about their own decision — the fastest
        way to get a useful prompt muted.
        """
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            decide_approval(
                run.id, "gate", ApprovalDecision.REJECTED, decided_by="operator", tenant_id=TENANT
            )
            resumed = await _engine(data).resume_run(run.id)
            assert resumed.status == RunStatus.CANCELLED
            assert "rejected by operator" in resumed.error_message.lower()
            assert "send" not in resumed.context["steps"]
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_a_rejected_cron_run_is_not_paged(self, no_alerts):
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            decide_approval(
                run.id, "gate", ApprovalDecision.REJECTED, decided_by="operator", tenant_id=TENANT
            )
            engine = _engine(data)
            with patch.object(engine, "_notify_run_failure") as notify:
                await engine.resume_run(run.id)
            notify.assert_not_called()
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_rejection_routes_to_on_reject_when_declared(self, no_alerts):
        data = _wf(on_reject="cleanup")
        data["steps"].append({"id": "cleanup", "type": "transform", "expression": "cleaned"})
        run = await _run(_engine(data), data["id"])
        try:
            decide_approval(
                run.id, "gate", ApprovalDecision.REJECTED, decided_by="operator", tenant_id=TENANT
            )
            resumed = await _engine(data).resume_run(run.id)
            assert resumed.status == RunStatus.COMPLETED
            assert resumed.context["steps"]["cleanup"]["status"] == "completed"
            assert "send" not in resumed.context["steps"]
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_resuming_an_undecided_run_changes_nothing(self, no_alerts):
        """The driver sweeps; it must not restart a run nobody answered."""
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            engine = _engine(data)
            # Nothing decided, so nothing to resume from — the run stays put.
            resumed = await engine.resume_run(run.id)
            assert resumed is not None
            assert resumed.status == RunStatus.AWAITING_APPROVAL
            assert get_approval(run.id, "gate", tenant_id=TENANT).status == "pending"
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_a_terminal_run_is_never_resumed_twice(self, no_alerts):
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            decide_approval(
                run.id, "gate", ApprovalDecision.APPROVED, decided_by="operator", tenant_id=TENANT
            )
            assert (await _engine(data).resume_run(run.id)).status == RunStatus.COMPLETED
            # Second sweep, same decided approval: the run is terminal now.
            assert await _engine(data).resume_run(run.id) is None
        finally:
            _cleanup(run.id)


class TestResumeDoesNotRepeatWork:
    @pytest.mark.asyncio
    async def test_steps_before_the_gate_do_not_run_a_second_time(self, no_alerts):
        """The resume point is load-bearing, and only a side effect proves it.

        Re-entering at step 0 would look identical for idempotent steps and
        would send the email twice for a real one. So the pre-gate step here
        counts its own executions: exactly one across suspend and resume.
        """
        data = _wf()
        data["steps"][0] = {
            "id": "prepare",
            "type": "tool",
            "tool_name": "send_the_thing",
            "tool_args": {},
        }
        calls: list[str] = []

        async def _fake_tool(self, step, run, result):
            calls.append(step.id)
            result.status = WorkflowStepStatus.COMPLETED
            result.output_text = "sent"

        with patch.object(WorkflowEngine, "_run_tool_step", _fake_tool):
            run = await _run(_engine(data), data["id"])
            try:
                assert calls == ["prepare"]

                decide_approval(
                    run.id,
                    "gate",
                    ApprovalDecision.APPROVED,
                    decided_by="operator",
                    tenant_id=TENANT,
                )
                resumed = await _engine(data).resume_run(run.id)

                assert resumed.status == RunStatus.COMPLETED
                assert calls == ["prepare"], (
                    f"pre-gate work repeated on resume: {calls} — a resumed run "
                    "must not re-send what it already sent"
                )
            finally:
                _cleanup(run.id)


class TestTimeout:
    @pytest.mark.asyncio
    async def test_unanswered_by_default_fails_the_run(self, no_alerts):
        """Silence is not consent. A gate that nobody answered did not get
        the decision it was written to require."""
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            _force_expiry(run.id, "gate")
            engine = _engine(data)
            await engine.drive_approvals()

            from robothor.db.connection import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT status FROM workflow_runs WHERE id = %s", (run.id,))
                assert cur.fetchone()[0] == "failed"
            assert get_approval(run.id, "gate", tenant_id=TENANT).status == "expired"
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_on_timeout_approve_is_opt_in_and_works(self, no_alerts):
        """Low-stakes gates may treat silence as consent — but only when the
        YAML says so, so the choice is visible where it is made."""
        data = _wf(on_timeout="approve")
        run = await _run(_engine(data), data["id"])
        try:
            _force_expiry(run.id, "gate")
            await _engine(data).drive_approvals()

            from robothor.db.connection import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT status FROM workflow_runs WHERE id = %s", (run.id,))
                assert cur.fetchone()[0] == "completed"
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_an_expired_question_is_kept_not_deleted(self, no_alerts):
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            _force_expiry(run.id, "gate")
            await _engine(data).drive_approvals()
            row = get_approval(run.id, "gate", tenant_id=TENANT)
            assert row is not None
            assert row.status == "expired"
            assert row.decided_by == "timeout"
        finally:
            _cleanup(run.id)


class TestDriver:
    @pytest.mark.asyncio
    async def test_the_sweep_resumes_a_decided_run(self, no_alerts):
        """Nobody calls resume_run by hand in production. If the driver does
        not find the decision, the feature is decoration."""
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            decide_approval(
                run.id, "gate", ApprovalDecision.APPROVED, decided_by="operator", tenant_id=TENANT
            )
            counts = await _engine(data).drive_approvals()
            assert counts["resumed"] >= 1

            from robothor.db.connection import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT status FROM workflow_runs WHERE id = %s", (run.id,))
                assert cur.fetchone()[0] == "completed"
        finally:
            _cleanup(run.id)

    @pytest.mark.asyncio
    async def test_a_quiet_sweep_is_harmless(self, no_alerts):
        data = _wf()
        counts = await _engine(data).drive_approvals()
        assert counts["resumed"] == 0

    @pytest.mark.asyncio
    async def test_one_unresumable_run_does_not_stop_the_others(self, no_alerts):
        """A workflow removed from disk while a run waited must not wedge the
        sweep for every other pending decision."""
        data = _wf()
        run = await _run(_engine(data), data["id"])
        try:
            decide_approval(
                run.id, "gate", ApprovalDecision.APPROVED, decided_by="operator", tenant_id=TENANT
            )
            # An engine that no longer knows this workflow: the file was
            # deleted or renamed between suspend and resume.
            engine = _engine(data)
            engine._workflows.clear()
            counts = await engine.drive_approvals()
            assert counts["unresumable"] >= 1
            assert counts["resumed"] == 0
        finally:
            _cleanup(run.id)


class TestParsing:
    def test_an_approval_step_requires_a_prompt(self):
        data = _wf()
        del data["steps"][1]["prompt"]
        with pytest.raises(ValueError, match="require a prompt"):
            parse_workflow(data)

    def test_on_timeout_must_be_a_known_policy(self):
        data = _wf(on_timeout="maybe")
        with pytest.raises(ValueError, match="on_timeout must be"):
            parse_workflow(data)

    def test_approval_cannot_hide_inside_a_parallel_branch(self):
        """One run has one resume point. Suspending a single branch would
        strand its siblings."""
        data = {
            "id": "bad",
            "steps": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "parallel_steps": [
                        {"id": "gate", "type": "approval", "prompt": "?"},
                    ],
                }
            ],
        }
        with pytest.raises(ValueError, match="cannot run inside parallel"):
            parse_workflow(data)


class TestNoDoublePaging:
    @pytest.mark.asyncio
    async def test_the_operator_is_asked_once_across_restarts(self):
        """A crash loop that re-asks every restart is how a prompt gets muted."""
        data = _wf()
        with patch("robothor.engine.workflow.WorkflowEngine._notify_approval_request") as notify:
            run = await _run(_engine(data), data["id"])
            try:
                assert notify.call_count == 1
                # Restart: a new engine re-enters the same waiting step.
                again = await _engine(data).resume_run(run.id)
                assert again.status == RunStatus.AWAITING_APPROVAL
                assert notify.call_count == 1  # still one
            finally:
                _cleanup(run.id)


def _force_expiry(run_id: str, step_id: str) -> None:
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_approvals SET expires_at = %s WHERE run_id = %s AND step_id = %s",
            (datetime.now(UTC) - timedelta(minutes=1), run_id, step_id),
        )
        conn.commit()


def _unused_status_import_guard() -> None:  # pragma: no cover
    """Keeps WorkflowStepStatus imported for readers of this module."""
    assert WorkflowStepStatus.WAITING == "waiting"

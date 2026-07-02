"""Phase 5 — end-to-end task lifecycle against a real PostgreSQL.

Five tests that walk the canonical multi-day workflow:

  1. create → plan → ask → answer → advance → close (the quote→PO loop)
  2. follow_up_at snooze + resurface
  3. a redundant same-state transition is an idempotent no-op (no dup history)
  4. reject spawns subtasks with correct parent_task_id + priority
  5. todo promotion creates idempotent subtasks

(The autonomy-budget refusal cases are pure unit tests and live in
``robothor/engine/tests/test_autonomy_refusal.py`` so the default CI gate runs
them.)

All tests use the existing ``mock_get_connection`` fixture from
``tests/conftest_integration.py`` which connects via
``ROBOTHOR_TEST_DB_DSN`` against the real PostgreSQL spun up by
``docker-compose up -d``. Run with::

    pytest tests/integration/test_task_lifecycle.py -m integration -v

Skipped in pre-commit (``pytest -m "not integration"``). All test data
uses Alice / Bob / agent@example.com per the no-personal-data rule, and
the ``default`` tenant via ``DEFAULT_TENANT``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.constants import DEFAULT_TENANT


@pytest.mark.integration
class TestFullLifecycle:
    def test_create_plan_ask_answer_advance_close(self, db_conn, mock_get_connection, test_prefix):
        """Full quote→PO loop. Each transition writes the expected `kind` history row."""
        from robothor.crm.dal import (
            answer_question,
            approve_task,
            create_task,
            get_task_history,
            set_next_action,
            set_question,
            update_task,
        )

        # 1. create — operator files a thread-tagged task with an objective
        task_id = create_task(
            title=f"{test_prefix} Confirm widget pricing",
            body="Initial inquiry to Alice at Acme Corp.",
            objective="Confirm pricing without scheduling a meeting",
            assigned_to_agent="email-responder",
            tags=["thread"],
            priority="high",
            tenant_id=DEFAULT_TENANT,
        )
        assert isinstance(task_id, str), f"create_task returned non-string: {task_id!r}"

        # 2. plan — planner picks the next concrete step
        assert set_next_action(
            task_id=task_id,
            next_action="Email Alice for written quote",
            agent="email-responder",
            by="planner",
            tenant_id=DEFAULT_TENANT,
        )
        history = get_task_history(task_id, tenant_id=DEFAULT_TENANT)
        kinds = [(h.get("metadata") or {}).get("kind") for h in history]
        assert "plan" in kinds

        # 3. ask — planner escalates a concrete question to the operator
        assert set_question(
            task_id=task_id,
            question="Drop Acme outreach and pivot to Bob? y/n",
            by="planner",
            tenant_id=DEFAULT_TENANT,
        )

        # escalation_count bumped, status now REVIEW
        cur = db_conn.cursor()
        cur.execute(
            "SELECT status, escalation_count, question_for_operator FROM crm_tasks WHERE id = %s",
            (task_id,),
        )
        row = cur.fetchone()
        assert row[0] == "REVIEW"
        assert row[1] >= 1
        assert "Drop Acme outreach" in (row[2] or "")

        # 4. answer — operator answers via the Phase-4 endpoint
        assert answer_question(
            task_id=task_id,
            answer="No, keep chasing Alice — give it another week",
            by="helm-user",
            advance_to="IN_PROGRESS",
            tenant_id=DEFAULT_TENANT,
        )

        cur.execute(
            "SELECT status, escalation_count, question_for_operator, "
            "requires_human, question_resolved_at, question_resolved_by "
            "FROM crm_tasks WHERE id = %s",
            (task_id,),
        )
        row = cur.fetchone()
        assert row[0] == "IN_PROGRESS"
        assert row[1] == 0  # reset after answer
        assert row[2] is None
        assert row[3] is False
        assert row[4] is not None  # question_resolved_at set
        assert row[5] == "helm-user"

        # 5. advance to REVIEW — worker says "ready for sign-off"
        assert (
            update_task(
                task_id=task_id,
                status="REVIEW",
                changed_by="email-responder",
                tenant_id=DEFAULT_TENANT,
            )
            is True
        )

        # 6. close — operator approves
        assert (
            approve_task(
                task_id=task_id,
                resolution="Quote received, signed off",
                reviewer="helm-user",
                tenant_id=DEFAULT_TENANT,
            )
            is True
        )

        cur.execute(
            "SELECT status, escalation_count FROM crm_tasks WHERE id = %s",
            (task_id,),
        )
        row = cur.fetchone()
        assert row[0] == "DONE"
        assert row[1] == 0

        # History chain is intact: plan → ask → answer → review → done
        history = get_task_history(task_id, tenant_id=DEFAULT_TENANT)
        kinds_seen = {(h.get("metadata") or {}).get("kind") for h in history}
        assert "plan" in kinds_seen
        assert "ask" in kinds_seen
        assert "answer" in kinds_seen


@pytest.mark.integration
class TestFollowUpResurface:
    def test_snooze_and_resurface(self, db_conn, mock_get_connection, test_prefix):
        """follow_up_at hides a task from list_threads until the due time passes."""
        from robothor.crm.dal import (
            create_task,
            resurface_due_followups,
            update_task,
        )

        task_id = create_task(
            title=f"{test_prefix} Snoozable",
            body="Wait for vendor reply",
            tags=["thread"],
            assigned_to_agent="email-responder",
            tenant_id=DEFAULT_TENANT,
        )
        assert isinstance(task_id, str)

        future = datetime.now(UTC) + timedelta(hours=1)
        assert (
            update_task(
                task_id=task_id,
                follow_up_at=future.isoformat(),
                changed_by="planner",
                tenant_id=DEFAULT_TENANT,
            )
            is True
        )

        # Backdate via direct UPDATE so resurface_due_followups picks it up.
        past = datetime.now(UTC) - timedelta(minutes=5)
        cur = db_conn.cursor()
        cur.execute(
            "UPDATE crm_tasks SET follow_up_at = %s WHERE id = %s",
            (past, task_id),
        )
        db_conn.commit()

        resurfaced = resurface_due_followups(tenant_id=DEFAULT_TENANT)
        assert task_id in resurfaced

        cur.execute("SELECT follow_up_at FROM crm_tasks WHERE id = %s", (task_id,))
        assert cur.fetchone()[0] is None


@pytest.mark.integration
class TestRedundantTransition:
    def test_redundant_transition_is_idempotent_noop(
        self, db_conn, mock_get_connection, test_prefix
    ):
        """Re-applying an already-applied status is an idempotent no-op.

        ``update_task`` adds ``status`` to the SET clause unconditionally, so a
        same-state IN_PROGRESS → IN_PROGRESS runs the UPDATE and returns True —
        there is no same-state guard. But it does NOT write a second
        transition-history row, since history is only recorded when
        ``current_status != new_status`` (dal.update_task). This is the real
        invariant: redundant transitions succeed without polluting history.
        """
        from robothor.crm.dal import create_task, update_task

        task_id = create_task(
            title=f"{test_prefix} Redundant target",
            body="x",
            tenant_id=DEFAULT_TENANT,
        )
        assert isinstance(task_id, str)

        # First move: TODO → IN_PROGRESS (a real transition, records history).
        assert (
            update_task(
                task_id=task_id,
                status="IN_PROGRESS",
                changed_by="agent-a",
                tenant_id=DEFAULT_TENANT,
            )
            is True
        )

        # Second move: IN_PROGRESS → IN_PROGRESS. Idempotent success (True),
        # not an error — and it must NOT append another transition row.
        assert (
            update_task(
                task_id=task_id,
                status="IN_PROGRESS",
                changed_by="agent-b",
                tenant_id=DEFAULT_TENANT,
            )
            is True
        )

        # Exactly one TODO → IN_PROGRESS history row exists (from the first
        # move); the redundant no-op wrote none.
        cur = db_conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM crm_task_history "
            "WHERE task_id = %s AND to_status = 'IN_PROGRESS'",
            (task_id,),
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.integration
class TestRejectSpawnsSubtasks:
    def test_change_requests_become_subtasks(self, db_conn, mock_get_connection, test_prefix):
        from robothor.crm.dal import (
            create_task,
            reject_task,
            update_task,
        )

        parent_id = create_task(
            title=f"{test_prefix} Parent in review",
            body="x",
            assigned_to_agent="email-responder",
            tenant_id=DEFAULT_TENANT,
        )
        assert isinstance(parent_id, str)

        # Move to REVIEW so reject_task can act on it.
        assert update_task(
            task_id=parent_id,
            status="IN_PROGRESS",
            changed_by="agent-a",
            tenant_id=DEFAULT_TENANT,
        )
        assert update_task(
            task_id=parent_id,
            status="REVIEW",
            changed_by="agent-a",
            tenant_id=DEFAULT_TENANT,
        )

        change_requests = ["Re-check the math", "Re-confirm vendor identity"]
        assert (
            reject_task(
                task_id=parent_id,
                reason="Two things need fixing",
                reviewer="helm-user",
                change_requests=change_requests,
                tenant_id=DEFAULT_TENANT,
            )
            is True
        )

        cur = db_conn.cursor()
        cur.execute(
            "SELECT id, title, priority, parent_task_id, status, assigned_to_agent "
            "FROM crm_tasks WHERE parent_task_id = %s ORDER BY title",
            (parent_id,),
        )
        children = cur.fetchall()
        assert len(children) == 2
        for child in children:
            assert child[2] == "high"
            assert child[3] == parent_id
            assert child[4] == "TODO"
            assert child[5] == "email-responder"

        # Parent flipped back to IN_PROGRESS and escalation_count reset.
        cur.execute(
            "SELECT status, escalation_count FROM crm_tasks WHERE id = %s",
            (parent_id,),
        )
        row = cur.fetchone()
        assert row[0] == "IN_PROGRESS"
        assert row[1] == 0


@pytest.mark.integration
class TestTodoPromotionIdempotency:
    def test_same_hash_does_not_duplicate(self, db_conn, mock_get_connection, test_prefix):
        from robothor.crm.dal import create_task
        from robothor.engine.todo_promotion import (
            compute_item_hash,
            promote_todo_to_subtask,
        )
        from robothor.engine.todolist import TodoItem

        parent_id = create_task(
            title=f"{test_prefix} Thread parent",
            body="x",
            tags=["thread"],
            assigned_to_agent="email-responder",
            priority="high",
            tenant_id=DEFAULT_TENANT,
        )
        assert isinstance(parent_id, str)

        parent = {
            "id": parent_id,
            "tags": ["thread"],
            "priority": "high",
            "assigned_to_agent": "email-responder",
        }
        item = TodoItem(
            content="Chase Alice for written quote",
            active_form="Chasing Alice",
            status="pending",
        )

        # promote_todo_to_subtask returns PromotionOutcome(subtask_id, created):
        # the first call creates (created=True), the second is an idempotent hit
        # that reuses the same subtask_id (created=False).
        first = promote_todo_to_subtask(
            parent=parent,
            item=item,
            agent_id="email-responder",
            run_id="run-A",
            tenant_id=DEFAULT_TENANT,
        )
        assert first.subtask_id is not None
        assert first.created is True

        second = promote_todo_to_subtask(
            parent=parent,
            item=item,
            agent_id="email-responder",
            run_id="run-B",
            tenant_id=DEFAULT_TENANT,
        )
        assert second.subtask_id == first.subtask_id, (
            "second promotion must reuse the existing subtask"
        )
        assert second.created is False, "idempotent hit must not create a new subtask"

        # Body carries the hash marker so future runs can look it up.
        cur = db_conn.cursor()
        cur.execute("SELECT body, tags FROM crm_tasks WHERE id = %s", (first.subtask_id,))
        body, tags = cur.fetchone()
        h = compute_item_hash(parent_id, item.content)
        assert f"todo_hash: {h}" in body
        assert "promoted_todo" in (tags or [])

"""Durable agent questions — integration tests against a real database.

The entire claim of this table is that a question survives the coroutine that
asked it. A test that mocked the store would certify the opposite of what is
being built, so every test here writes real rows and reads them back through an
independent call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from robothor.engine.agent_questions import (
    answer_question,
    ask_question,
    expire_overdue_questions,
    get_question,
    list_pending_questions,
)

pytestmark = pytest.mark.integration

TENANT = "test-tenant-agent-questions"


@pytest.fixture
def run_id() -> str:
    return str(uuid.uuid4())


def _cleanup(run_id: str) -> None:
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent_questions WHERE run_id = %s", (run_id,))
        conn.commit()


def _set_expiry(question_id: str, when: datetime) -> None:
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE agent_questions SET expires_at = %s WHERE id = %s", (when, question_id))
        conn.commit()


class TestAskAndRead:
    def test_a_question_is_a_row_that_outlives_the_caller(self, run_id):
        try:
            asked = ask_question(
                run_id=run_id,
                agent_id="assistant",
                question="Which vendor should I renew with?",
                options=["Acme", "Globex"],
                channel="telegram",
                target="chat-placeholder",
                timeout_seconds=300,
                tenant_id=TENANT,
            )
            assert asked.status == "pending"
            assert asked.options == ["Acme", "Globex"]

            fetched = get_question(asked.id, tenant_id=TENANT)
            assert fetched is not None
            assert fetched.question == "Which vendor should I renew with?"
            assert fetched.kind == "question"
            assert fetched.channel == "telegram"
            assert fetched.answer is None
        finally:
            _cleanup(run_id)

    def test_one_run_may_ask_twice(self, run_id):
        """No UNIQUE (run_id, ...): "which one?" then "are you sure?"."""
        try:
            first = ask_question(
                run_id=run_id,
                agent_id="assistant",
                question="Which one?",
                tenant_id=TENANT,
            )
            second = ask_question(
                run_id=run_id,
                agent_id="assistant",
                question="Are you sure?",
                tenant_id=TENANT,
            )
            assert first.id != second.id
            pending = [q for q in list_pending_questions(tenant_id=TENANT) if q.run_id == run_id]
            assert len(pending) == 2
        finally:
            _cleanup(run_id)

    def test_an_escalation_is_the_same_shape_under_a_different_kind(self, run_id):
        try:
            asked = ask_question(
                run_id=run_id,
                agent_id="assistant",
                question="Approve exec?",
                kind="escalation",
                tenant_id=TENANT,
            )
            assert get_question(asked.id, tenant_id=TENANT).kind == "escalation"
        finally:
            _cleanup(run_id)


class TestAnswer:
    def test_the_first_answer_wins_and_a_second_changes_nothing(self, run_id):
        try:
            asked = ask_question(
                run_id=run_id, agent_id="assistant", question="When?", tenant_id=TENANT
            )
            assert answer_question(asked.id, "Tuesday", answered_by="operator", tenant_id=TENANT)
            # A double-tap, a retried POST, a second operator: all no-ops.
            assert not answer_question(
                asked.id, "Wednesday", answered_by="operator", tenant_id=TENANT
            )

            settled = get_question(asked.id, tenant_id=TENANT)
            assert settled.status == "answered"
            assert settled.answer == "Tuesday"
            assert settled.answered_by == "operator"
            assert settled.answered_at is not None
        finally:
            _cleanup(run_id)

    def test_an_answered_question_leaves_the_pending_list(self, run_id):
        try:
            asked = ask_question(
                run_id=run_id, agent_id="assistant", question="When?", tenant_id=TENANT
            )
            answer_question(asked.id, "Tuesday", answered_by="operator", tenant_id=TENANT)
            assert not [q for q in list_pending_questions(tenant_id=TENANT) if q.run_id == run_id]
        finally:
            _cleanup(run_id)

    def test_answering_an_unknown_id_is_false_not_an_error(self):
        assert not answer_question(
            str(uuid.uuid4()), "hello", answered_by="operator", tenant_id=TENANT
        )


class TestExpiry:
    def test_an_overdue_question_is_stamped_expired_and_kept(self, run_id):
        try:
            asked = ask_question(
                run_id=run_id, agent_id="assistant", question="When?", tenant_id=TENANT
            )
            _set_expiry(asked.id, datetime.now(UTC) - timedelta(minutes=5))

            expired = expire_overdue_questions(tenant_id=TENANT)
            assert asked.id in [q.id for q in expired]

            # KEPT, not deleted: "nobody answered" is the fact worth having.
            row = get_question(asked.id, tenant_id=TENANT)
            assert row is not None
            assert row.status == "expired"
            assert row.answer is None
        finally:
            _cleanup(run_id)

    def test_an_expired_question_can_still_be_answered_out_of_band(self, run_id):
        """The tool stopped waiting; the operator has not stopped caring.

        An expired row that refused a late answer would throw away the only
        useful thing left about it.
        """
        try:
            asked = ask_question(
                run_id=run_id, agent_id="assistant", question="When?", tenant_id=TENANT
            )
            _set_expiry(asked.id, datetime.now(UTC) - timedelta(minutes=5))
            expire_overdue_questions(tenant_id=TENANT)

            assert answer_question(asked.id, "Tuesday", answered_by="operator", tenant_id=TENANT)
            assert get_question(asked.id, tenant_id=TENANT).status == "answered"
        finally:
            _cleanup(run_id)

    def test_a_question_still_inside_its_deadline_is_untouched(self, run_id):
        try:
            asked = ask_question(
                run_id=run_id,
                agent_id="assistant",
                question="When?",
                timeout_seconds=3600,
                tenant_id=TENANT,
            )
            expire_overdue_questions(tenant_id=TENANT)
            assert get_question(asked.id, tenant_id=TENANT).status == "pending"
        finally:
            _cleanup(run_id)


class TestTheMigrationItself:
    def test_it_is_idempotent_against_a_database_that_already_has_the_table(self):
        """Every deploy re-runs the ledger, and a migration that aborted the
        second time would wedge the chain for everyone after it. Applied here
        against the live test database, which already has it."""
        from pathlib import Path

        from robothor.db.connection import get_connection

        sql = (
            Path(__file__).resolve().parents[3] / "crm" / "migrations" / "117_agent_questions.sql"
        ).read_text(encoding="utf-8")

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()

    def test_the_status_check_refuses_a_state_nothing_declares(self):
        """A typo'd status has to fail at write time, not become a fourth
        state every reader silently ignores."""
        import psycopg2

        from robothor.db.connection import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO agent_questions (tenant_id, run_id, question, status, "
                    "expires_at) VALUES (%s, gen_random_uuid(), 'x', 'maybe', NOW())",
                    (TENANT,),
                )
            except psycopg2.errors.CheckViolation:
                conn.rollback()
            else:  # pragma: no cover - the CHECK is present
                conn.rollback()
                raise AssertionError("agent_questions accepted an undeclared status")

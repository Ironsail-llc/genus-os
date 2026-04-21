"""Tests for requires_human flag in CRM task DAL functions."""

from __future__ import annotations

from datetime import UTC
from typing import Any
from unittest.mock import MagicMock, patch

# We mock get_connection so no real DB is needed.


def _make_mock_conn(fetchone_return=None, fetchall_return=None, rowcount=1):
    """Build a mock connection + cursor for DAL tests."""
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = fetchone_return
    mock_cur.fetchall.return_value = fetchall_return or []
    mock_cur.rowcount = rowcount

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


class TestCreateTaskRequiresHuman:
    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_create_task_with_requires_human_true(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_task

        task_id = create_task(
            title="Test task",
            requires_human=True,
        )

        assert task_id is not None
        call_args = mock_cur.execute.call_args_list[0]
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "requires_human" in sql
        assert True in params

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_create_task_requires_human_defaults_false(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_task

        create_task(title="Normal task")

        call_args = mock_cur.execute.call_args_list[0]
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "requires_human" in sql
        assert False in params


class TestResolveTaskRequiresHumanGuard:
    @patch("robothor.crm.dal.get_connection")
    def test_resolve_requires_human_task_by_agent_blocked(self, mock_get_conn):
        """Agents cannot resolve requires_human tasks."""
        mock_conn, mock_cur = _make_mock_conn(
            fetchone_return={"status": "IN_PROGRESS", "requires_human": True}
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import resolve_task

        result = resolve_task(
            task_id="task-123",
            resolution="Auto-resolved",
            agent_id="email-classifier",
        )

        assert isinstance(result, dict)
        assert "error" in result
        assert "requires human" in result["error"].lower()

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_resolve_requires_human_task_by_operator_allowed(self, _audit, mock_get_conn):
        """The operator (helm-user) can resolve requires_human tasks."""
        mock_conn, mock_cur = _make_mock_conn(
            fetchone_return={"status": "IN_PROGRESS", "requires_human": True}
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import resolve_task

        result = resolve_task(
            task_id="task-123",
            resolution="Operator decided",
            agent_id="helm-user",
        )

        # Should succeed (True), not return error dict
        assert result is True

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_resolve_normal_task_by_agent_allowed(self, _audit, mock_get_conn):
        """Normal tasks can be resolved by any agent."""
        mock_conn, mock_cur = _make_mock_conn(
            fetchone_return={"status": "IN_PROGRESS", "requires_human": False}
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import resolve_task

        result = resolve_task(
            task_id="task-456",
            resolution="Auto-resolved: stale",
            agent_id="task-cleanup",
        )

        assert result is True


class TestListTasksRequiresHumanFilter:
    @patch("robothor.crm.dal.get_connection")
    def test_list_tasks_filter_requires_human_true(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import list_tasks

        list_tasks(requires_human=True)

        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "requires_human = %s" in sql
        assert True in params

    @patch("robothor.crm.dal.get_connection")
    def test_list_tasks_filter_requires_human_false(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import list_tasks

        list_tasks(requires_human=False)

        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "requires_human = %s" in sql
        assert False in params

    @patch("robothor.crm.dal.get_connection")
    def test_list_tasks_no_requires_human_filter(self, mock_get_conn):
        """When requires_human is None, no filter is applied."""
        mock_conn, mock_cur = _make_mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import list_tasks

        list_tasks(requires_human=None)

        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        assert "requires_human" not in sql


class TestTaskToDictRequiresHuman:
    def test_task_to_dict_includes_requires_human(self):
        from robothor.crm.models import task_to_dict

        row: dict[str, Any] = {
            "id": "abc-123",
            "title": "Test",
            "body": "",
            "status": "TODO",
            "due_at": None,
            "person_id": None,
            "company_id": None,
            "created_by_agent": "test",
            "assigned_to_agent": "main",
            "priority": "normal",
            "tags": [],
            "parent_task_id": None,
            "resolved_at": None,
            "resolution": "",
            "sla_deadline_at": None,
            "escalation_count": 0,
            "started_at": None,
            "tenant_id": "test-tenant",
            "updated_at": None,
            "created_at": None,
            "requires_human": True,
        }
        result = task_to_dict(row)
        assert result["requiresHuman"] is True

    def test_task_to_dict_requires_human_defaults_false(self):
        from robothor.crm.models import task_to_dict

        row: dict[str, Any] = {
            "id": "abc-123",
            "title": "Test",
            "body": "",
            "status": "TODO",
            "due_at": None,
            "person_id": None,
            "company_id": None,
            "created_by_agent": "test",
            "assigned_to_agent": "main",
            "priority": "normal",
            "tags": [],
            "parent_task_id": None,
            "resolved_at": None,
            "resolution": "",
            "sla_deadline_at": None,
            "escalation_count": 0,
            "started_at": None,
            "tenant_id": "test-tenant",
            "updated_at": None,
            "created_at": None,
            # requires_human not present — should default to False
        }
        result = task_to_dict(row)
        assert result["requiresHuman"] is False


class TestCreateTaskPlannerFields:
    """Stage 4 — structured task fields for the forward planner."""

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_create_task_persists_objective_and_next_action(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_task

        task_id = create_task(
            title="DrFirst pricing",
            objective="Confirm RxHistory pricing without scheduling a meeting",
            next_action="Email April asking for a written quote by EOW",
            next_action_agent="email-responder",
            question_for_operator=None,
            autonomy_budget={"reversible_cap_usd": 500},
        )

        assert task_id is not None
        insert_call = mock_cur.execute.call_args_list[0]
        sql = insert_call[0][0]
        params = insert_call[0][1]
        assert "objective" in sql
        assert "next_action" in sql
        assert "next_action_agent" in sql
        assert "question_for_operator" in sql
        assert "autonomy_budget" in sql
        assert "Confirm RxHistory pricing without scheduling a meeting" in params
        assert "Email April asking for a written quote by EOW" in params
        assert "email-responder" in params


class TestSetNextAction:
    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_set_next_action_writes_field_and_records_history(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(
            fetchone_return={"status": "IN_PROGRESS"},
            rowcount=1,
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import set_next_action

        ok = set_next_action(
            task_id="task-777",
            next_action="chase vendor for pricing",
            agent="email-responder",
            by="planner",
        )

        assert ok is True
        # Execute calls: SELECT status → UPDATE → INSERT history
        update_sqls = [
            c[0][0] for c in mock_cur.execute.call_args_list if "UPDATE crm_tasks" in c[0][0]
        ]
        assert update_sqls, "expected an UPDATE crm_tasks call"
        joined = " ".join(update_sqls)
        assert "next_action" in joined
        assert "next_action_agent" in joined
        assert "last_planned_at" in joined
        history_calls = [
            c for c in mock_cur.execute.call_args_list if "crm_task_history" in c[0][0]
        ]
        assert history_calls, "expected a crm_task_history row to be written"


class TestSetQuestion:
    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_set_question_writes_question_and_flips_requires_human(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(
            fetchone_return={"status": "IN_PROGRESS", "escalation_count": 0},
            rowcount=1,
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import set_question

        ok = set_question(
            task_id="task-888",
            question="Drop DrFirst vendor outreach y/n?",
            by="planner",
        )

        assert ok is True
        update_sqls = [
            c[0][0] for c in mock_cur.execute.call_args_list if "UPDATE crm_tasks" in c[0][0]
        ]
        assert update_sqls, "expected an UPDATE crm_tasks call"
        joined = " ".join(update_sqls)
        assert "question_for_operator" in joined
        assert "requires_human" in joined
        assert "status" in joined
        assert "escalation_count" in joined


class TestTaskToDictPlannerFields:
    def test_task_to_dict_exposes_planner_fields(self):
        from robothor.crm.models import task_to_dict

        row: dict[str, Any] = {
            "id": "abc-123",
            "title": "Test",
            "status": "TODO",
            "objective": "goal text",
            "next_action": "next step",
            "next_action_agent": "email-responder",
            "blockers": [{"kind": "awaiting_reply", "since": "2026-04-17T00:00:00Z"}],
            "question_for_operator": "approve X?",
            "autonomy_budget": {"reversible_cap_usd": 500},
            "last_planned_at": None,
            "planner_version": 1,
        }
        result = task_to_dict(row)
        assert result["objective"] == "goal text"
        assert result["nextAction"] == "next step"
        assert result["nextActionAgent"] == "email-responder"
        assert result["blockers"] == [{"kind": "awaiting_reply", "since": "2026-04-17T00:00:00Z"}]
        assert result["questionForOperator"] == "approve X?"
        assert result["autonomyBudget"] == {"reversible_cap_usd": 500}
        assert result["plannerVersion"] == 1

    def test_task_to_dict_planner_fields_default_null(self):
        from robothor.crm.models import task_to_dict

        row: dict[str, Any] = {"id": "abc-123", "title": "Test", "status": "TODO"}
        result = task_to_dict(row)
        assert result["objective"] is None
        assert result["nextAction"] is None
        assert result["nextActionAgent"] is None
        assert result["blockers"] == []
        assert result["questionForOperator"] is None
        assert result["autonomyBudget"] == {}
        assert result["plannerVersion"] == 0


class TestFindTaskByDedupKeyAgentScoped:
    """Verify that task dedup respects assigned_to_agent when provided."""

    @patch("robothor.crm.dal.get_connection")
    def test_dedup_with_agent_filters_by_agent(self, mock_get_conn):
        """find_task_by_dedup_key with assigned_to_agent adds agent filter to SQL."""
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import find_task_by_dedup_key

        find_task_by_dedup_key("threadId", "abc123", assigned_to_agent="email-responder")

        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "assigned_to_agent = %s" in sql
        assert "email-responder" in params

    @patch("robothor.crm.dal.get_connection")
    def test_dedup_without_agent_no_filter(self, mock_get_conn):
        """find_task_by_dedup_key without assigned_to_agent has no agent filter."""
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import find_task_by_dedup_key

        find_task_by_dedup_key("threadId", "abc123")

        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        assert "assigned_to_agent" not in sql

    @patch("robothor.crm.dal.get_connection")
    def test_thread_id_wrapper_passes_agent(self, mock_get_conn):
        """find_task_by_thread_id forwards assigned_to_agent to find_task_by_dedup_key."""
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import find_task_by_thread_id

        find_task_by_thread_id("abc123", assigned_to_agent="email-analyst")

        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "assigned_to_agent = %s" in sql
        assert "email-analyst" in params

    @patch("robothor.crm.dal.get_connection")
    def test_different_agents_same_thread_not_deduplicated(self, mock_get_conn):
        """Tasks for different agents with same threadId should NOT deduplicate."""
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import find_task_by_thread_id

        # Searching for email-responder task should not find email-analyst task
        result = find_task_by_thread_id("abc123", assigned_to_agent="email-responder")

        assert result is None  # No task found for this agent
        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        # Verify the query scopes to the specific agent
        assert "assigned_to_agent = %s" in sql


class TestFollowUpAtRoundTrip:
    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_create_task_accepts_follow_up_at(self, _audit, mock_get_conn):
        """create_task stores follow_up_at in the INSERT."""
        from datetime import datetime

        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_task

        future = datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)
        create_task(title="check signature", follow_up_at=future)

        # First execute is the INSERT — verify follow_up_at is in the SQL and params.
        insert_sql = mock_cur.execute.call_args_list[0][0][0]
        insert_params = mock_cur.execute.call_args_list[0][0][1]
        assert "follow_up_at" in insert_sql
        assert future in insert_params

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_list_tasks_excludes_snoozing_by_default(self, _audit, mock_get_conn):
        """list_tasks adds a filter clause so future follow_up_at rows don't surface."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import list_tasks

        list_tasks()
        sql = mock_cur.execute.call_args[0][0]
        assert "follow_up_at IS NULL OR follow_up_at <= NOW()" in sql

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_list_tasks_include_snoozed_disables_filter(self, _audit, mock_get_conn):
        """include_snoozed=True removes the follow_up_at filter."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import list_tasks

        list_tasks(include_snoozed=True)
        sql = mock_cur.execute.call_args[0][0]
        assert "follow_up_at" not in sql


class TestResurfaceDueFollowups:
    @patch("robothor.crm.dal.get_connection")
    def test_no_due_rows_returns_empty(self, mock_get_conn):
        """When nothing is due, returns [] and commits."""
        mock_conn, mock_cur = _make_mock_conn(fetchall_return=[])
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import resurface_due_followups

        assert resurface_due_followups(tenant_id="t1") == []
        mock_conn.commit.assert_called()

    @patch("robothor.crm.dal._record_transition")
    @patch("robothor.crm.dal.get_connection")
    def test_due_rows_cleared_and_history_written(self, mock_get_conn, mock_record):
        """When follow_up_at <= NOW, the field is cleared and a history row is written."""
        from datetime import datetime

        past = datetime(2020, 1, 1, tzinfo=UTC)
        mock_conn, mock_cur = _make_mock_conn(
            fetchall_return=[
                {"id": "task-1", "status": "TODO", "follow_up_at": past},
                {"id": "task-2", "status": "IN_PROGRESS", "follow_up_at": past},
            ]
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import resurface_due_followups

        ids = resurface_due_followups(tenant_id="t1")
        assert ids == ["task-1", "task-2"]

        # Second execute should be the UPDATE that clears follow_up_at
        update_call = mock_cur.execute.call_args_list[1]
        assert "follow_up_at = NULL" in update_call[0][0]
        # Verify uuid cast is in the SQL so postgres accepts string ids
        assert "uuid[]" in update_call[0][0]

        # Two history rows written (one per resurfaced task)
        assert mock_record.call_count == 2
        for call in mock_record.call_args_list:
            assert call.kwargs["changed_by"] == "system:follow_up"
            assert "resurfaced from follow_up_at=" in call.kwargs["reason"]

"""Tests for requires_human flag in CRM task DAL functions."""

from __future__ import annotations

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
        # Check that requires_human=True was passed in the INSERT
        call_args = mock_cur.execute.call_args_list[0]
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "requires_human" in sql
        # requires_human is the second-to-last param (before tenant_id)
        assert params[-2] is True

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_create_task_requires_human_defaults_false(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_task

        create_task(title="Normal task")

        call_args = mock_cur.execute.call_args_list[0]
        params = call_args[0][1]
        # requires_human should be False by default (second-to-last param)
        assert params[-2] is False


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
    def test_resolve_requires_human_task_by_philip_allowed(self, _audit, mock_get_conn):
        """Philip (helm-user) can resolve requires_human tasks."""
        mock_conn, mock_cur = _make_mock_conn(
            fetchone_return={"status": "IN_PROGRESS", "requires_human": True}
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import resolve_task

        result = resolve_task(
            task_id="task-123",
            resolution="Philip decided",
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

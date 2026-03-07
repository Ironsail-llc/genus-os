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
            fetchone_return={"status": "TODO", "requires_human": True}
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
            fetchone_return={"status": "TODO", "requires_human": True}
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
            fetchone_return={"status": "TODO", "requires_human": False}
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
            "tenant_id": "robothor-primary",
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
            "tenant_id": "robothor-primary",
            "updated_at": None,
            "created_at": None,
            # requires_human not present — should default to False
        }
        result = task_to_dict(row)
        assert result["requiresHuman"] is False

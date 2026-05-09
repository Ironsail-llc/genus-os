"""Tests for the session_goal DAL helpers.

A session goal is a crm_task with the `session_goal` tag. Workspace-scoped
goals carry only the `session_goal` tag; agent-scoped goals also carry
`agent:<agent_id>`. The structured payload (success criteria, evidence,
completion note) lives in `session_goal_meta` (JSONB).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


def _make_mock_conn(fetchone_return=None, fetchall_return=None, rowcount=1):
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = fetchone_return
    mock_cur.fetchall.return_value = fetchall_return or []
    mock_cur.rowcount = rowcount

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


def _goal_row(
    *,
    task_id: str = "task-1",
    objective: str = "Ship the session goal feature",
    tags: list[str] | None = None,
    status: str = "TODO",
    meta: dict[str, Any] | None = None,
    tenant_id: str = "default",
) -> dict[str, Any]:
    """Build a fake crm_tasks row with session_goal columns populated."""
    return {
        "id": task_id,
        "title": objective[:100],
        "body": "",
        "status": status,
        "due_at": None,
        "person_id": None,
        "company_id": None,
        "created_by_agent": "operator",
        "assigned_to_agent": "main",
        "priority": "high",
        "tags": tags or ["session_goal"],
        "parent_task_id": None,
        "resolved_at": None,
        "resolution": None,
        "sla_deadline_at": None,
        "escalation_count": 0,
        "started_at": None,
        "tenant_id": tenant_id,
        "updated_at": None,
        "created_at": None,
        "requires_human": False,
        "objective": objective,
        "next_action": None,
        "next_action_agent": None,
        "blockers": [],
        "question_for_operator": None,
        "autonomy_budget": {},
        "last_planned_at": None,
        "planner_version": 0,
        "session_goal_meta": meta
        or {
            "success_criteria": ["criterion 1"],
            "evidence": [],
            "completion_note": "",
        },
    }


class TestGetActiveSessionGoal:
    @patch("robothor.crm.dal.get_connection")
    def test_returns_workspace_goal_when_no_agent_id(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(
            fetchone_return=_goal_row(task_id="t-ws", tags=["session_goal"])
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_active_session_goal

        goal = get_active_session_goal(tenant_id="default")

        assert goal is not None
        assert goal["id"] == "t-ws"
        sql = mock_cur.execute.call_args[0][0]
        # Filter on session_goal tag must appear in the query.
        assert "session_goal" in sql or "tags" in sql

    @patch("robothor.crm.dal.get_connection")
    def test_returns_agent_scoped_goal_when_agent_id_passed(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(
            fetchone_return=_goal_row(task_id="t-main", tags=["session_goal", "agent:main"])
        )
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_active_session_goal

        goal = get_active_session_goal(tenant_id="default", agent_id="main")

        assert goal is not None
        assert goal["id"] == "t-main"
        params = list(mock_cur.execute.call_args[0][1])
        # The agent tag must be in the params for the @> filter.
        flat = []
        for p in params:
            if isinstance(p, list):
                flat.extend(p)
            else:
                flat.append(p)
        assert "agent:main" in flat

    @patch("robothor.crm.dal.get_connection")
    def test_returns_none_when_no_active_goal(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_active_session_goal

        assert get_active_session_goal(tenant_id="default") is None

    @patch("robothor.crm.dal.get_connection")
    def test_does_not_return_completed_goals(self, mock_get_conn):
        # The DAL filter must exclude DONE/CANCELED. We assert by SQL, not by
        # caller-side filtering, so a row coming back is honoured.
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_active_session_goal

        get_active_session_goal(tenant_id="default")
        sql = mock_cur.execute.call_args[0][0]
        assert "DONE" in sql and "CANCELED" in sql

    @patch("robothor.crm.dal.get_connection")
    def test_workspace_lookup_excludes_agent_scoped_rows(self, mock_get_conn):
        """When agent_id='', we want workspace-only goals. The SQL must filter
        out rows that carry an agent:* tag so an agent-scoped goal doesn't
        leak into the workspace lookup."""
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_active_session_goal

        get_active_session_goal(tenant_id="default")
        sql = mock_cur.execute.call_args[0][0]
        # The lookup must exclude rows whose tags contain any 'agent:%' value.
        assert "agent:" in sql.lower() or "NOT" in sql

    @patch("robothor.crm.dal.get_connection")
    def test_tenant_isolation(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import get_active_session_goal

        get_active_session_goal(tenant_id="tenant-A")
        params = mock_cur.execute.call_args[0][1]
        assert "tenant-A" in list(params)


class TestCreateSessionGoal:
    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_creates_workspace_goal_with_session_goal_tag(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_session_goal

        task_id = create_session_goal(
            tenant_id="default",
            objective="Ship the session goal feature",
            success_criteria=["one", "two"],
        )

        assert task_id is not None
        # The INSERT should carry session_goal in the tags array.
        insert_call = next(
            c for c in mock_cur.execute.call_args_list if "INSERT" in c[0][0].upper()
        )
        params = insert_call[0][1]
        flat = []
        for p in params:
            if isinstance(p, list):
                flat.extend(p)
            else:
                flat.append(p)
        assert "session_goal" in flat
        assert "agent:" not in [s for s in flat if isinstance(s, str)]

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_creates_agent_scoped_goal_with_agent_tag(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_session_goal

        create_session_goal(
            tenant_id="default",
            objective="agent objective",
            success_criteria=["one"],
            agent_id="email-classifier",
        )

        insert_call = next(
            c for c in mock_cur.execute.call_args_list if "INSERT" in c[0][0].upper()
        )
        params = insert_call[0][1]
        flat = []
        for p in params:
            if isinstance(p, list):
                flat.extend(p)
            else:
                flat.append(p)
        assert "session_goal" in flat
        assert "agent:email-classifier" in flat

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_persists_objective_and_meta(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_session_goal

        create_session_goal(
            tenant_id="default",
            objective="Big objective",
            success_criteria=["a", "b"],
        )

        # Find the meta-write SQL (either a UPDATE on session_goal_meta or
        # the INSERT itself if implementation pushes it inline).
        all_sql = " | ".join(c[0][0] for c in mock_cur.execute.call_args_list)
        all_params = []
        for c in mock_cur.execute.call_args_list:
            all_params.extend(list(c[0][1]) if len(c[0]) > 1 else [])
        # Either way: the JSON payload must include success_criteria.
        flat_strs = [str(p) for p in all_params]
        assert any("success_criteria" in s for s in flat_strs), all_sql


class TestAddSessionGoalEvidence:
    @patch("robothor.crm.dal.get_connection")
    def test_appends_to_meta_evidence_list(self, mock_get_conn):
        existing = _goal_row(
            meta={
                "success_criteria": ["c1"],
                "evidence": [
                    {
                        "kind": "note",
                        "summary": "earlier",
                        "reference": "",
                        "recorded_at": "2026-01-01T00:00:00+00:00",
                        "valid": True,
                    }
                ],
                "completion_note": "",
            }
        )
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=existing)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import add_session_goal_evidence

        ok = add_session_goal_evidence(
            task_id="task-1",
            kind="test_run",
            summary="all green",
            reference="pytest:passed:42",
            valid=True,
            tenant_id="default",
        )

        assert ok is True
        # Find the UPDATE setting session_goal_meta and inspect its JSON payload.
        update_calls = [
            c for c in mock_cur.execute.call_args_list if "session_goal_meta" in c[0][0]
        ]
        assert update_calls, "expected an UPDATE setting session_goal_meta"
        payload = next(p for p in update_calls[-1][0][1] if isinstance(p, str) and "evidence" in p)
        assert "test_run" in payload
        assert "pytest:passed:42" in payload
        # Earlier evidence must be preserved.
        assert "earlier" in payload

    @patch("robothor.crm.dal.get_connection")
    def test_returns_false_when_task_missing(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import add_session_goal_evidence

        ok = add_session_goal_evidence(
            task_id="missing",
            kind="note",
            summary="x",
            reference="",
            valid=True,
            tenant_id="default",
        )
        assert ok is False


class TestCompleteSessionGoal:
    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_sets_status_done_and_writes_completion_note(self, _audit, mock_get_conn):
        existing = _goal_row(
            meta={
                "success_criteria": ["c1"],
                "evidence": [
                    {
                        "kind": "test_run",
                        "summary": "ok",
                        "reference": "pytest:passed:1",
                        "recorded_at": "2026-05-09T00:00:00+00:00",
                        "valid": True,
                    },
                    {
                        "kind": "commit",
                        "summary": "shipped",
                        "reference": "abcdef1234567",
                        "recorded_at": "2026-05-09T00:00:00+00:00",
                        "valid": True,
                    },
                ],
                "completion_note": "",
            }
        )
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=existing)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import complete_session_goal

        ok = complete_session_goal(
            task_id="task-1",
            completion_note="feature shipped",
            tenant_id="default",
        )
        assert ok is True
        # An UPDATE must set status DONE for this task.
        all_sql = " | ".join(c[0][0] for c in mock_cur.execute.call_args_list)
        assert "DONE" in all_sql
        # Completion note must be persisted into the meta payload.
        all_params = []
        for c in mock_cur.execute.call_args_list:
            all_params.extend(list(c[0][1]) if len(c[0]) > 1 else [])
        assert any("feature shipped" in str(p) for p in all_params)

    @patch("robothor.crm.dal.get_connection")
    def test_returns_false_when_task_missing(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import complete_session_goal

        ok = complete_session_goal(
            task_id="missing",
            completion_note="x",
            tenant_id="default",
        )
        assert ok is False


class TestUpdateSessionGoalMeta:
    @patch("robothor.crm.dal.get_connection")
    def test_replaces_meta_jsonb(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import update_session_goal_meta

        new_meta = {
            "success_criteria": ["new"],
            "evidence": [],
            "completion_note": "",
        }
        ok = update_session_goal_meta(
            task_id="task-1",
            meta=new_meta,
            tenant_id="default",
        )
        assert ok is True
        sql = mock_cur.execute.call_args[0][0]
        assert "session_goal_meta" in sql
        params = mock_cur.execute.call_args[0][1]
        # Must be JSON-serialised.
        assert any("success_criteria" in str(p) for p in params)

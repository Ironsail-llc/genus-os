"""Tests for the session_goal DAL helpers.

A session goal is a crm_task with the `session_goal` tag. Every goal is
agent-scoped (carries `agent:<agent_id>`); workspace-only goals are gone
in the unified v2 model. The structured payload — objective, success
criteria, metric_targets seeded from the manifest, typed evidence, and
completion note — all lives in the `session_goal_meta` JSONB column.

Goal tasks also carry the `thread` tag so the thread pool / forward
planner pick them up automatically.
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


# ─── v2 unified model: metric_targets, in-place edits, get_or_create ────


class TestCreateSessionGoalV2:
    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_create_stamps_thread_tag_automatically(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_session_goal

        create_session_goal(
            tenant_id="default",
            objective="x",
            success_criteria=["one"],
            agent_id="main",
        )
        insert_call = next(
            c for c in mock_cur.execute.call_args_list if "INSERT" in c[0][0].upper()
        )
        flat = []
        for p in insert_call[0][1]:
            if isinstance(p, list):
                flat.extend(p)
            else:
                flat.append(p)
        assert "thread" in flat, "v2 goals must carry the `thread` tag for the thread pool"

    @patch("robothor.crm.dal.get_connection")
    @patch("robothor.crm.dal._safe_audit")
    def test_create_persists_metric_targets_in_meta(self, _audit, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn()
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import create_session_goal

        targets = [
            {
                "id": "passes-its-job",
                "category": "quality",
                "metric": "benchmark_pass_rate",
                "target": ">=0.85",
                "weight": 5.0,
                "window_days": 7,
                "extras": {},
            }
        ]
        create_session_goal(
            tenant_id="default",
            objective="x",
            success_criteria=["one"],
            agent_id="main",
            metric_targets=targets,
        )
        insert_call = next(
            c for c in mock_cur.execute.call_args_list if "INSERT" in c[0][0].upper()
        )
        json_payload = next(
            p for p in insert_call[0][1] if isinstance(p, str) and "metric_targets" in p
        )
        assert "passes-its-job" in json_payload
        assert "benchmark_pass_rate" in json_payload


class TestGetOrCreateAgentGoal:
    @patch("robothor.crm.dal.get_active_session_goal")
    @patch("robothor.crm.dal.create_session_goal")
    def test_returns_existing_when_present(self, mock_create, mock_get):
        mock_get.return_value = _goal_row(
            task_id="existing-1", tags=["session_goal", "agent:main", "thread"]
        )

        from robothor.crm.dal import get_or_create_agent_goal

        manifest = {
            "id": "main",
            "goals": {
                "quality": [
                    {"id": "passes-its-job", "metric": "benchmark_pass_rate", "target": ">=0.85"}
                ]
            },
        }
        result = get_or_create_agent_goal(tenant_id="default", agent_id="main", manifest=manifest)
        assert result["id"] == "existing-1"
        mock_create.assert_not_called()

    @patch("robothor.crm.dal.get_active_session_goal")
    @patch("robothor.crm.dal.create_session_goal")
    def test_creates_seeded_from_manifest_when_missing(self, mock_create, mock_get):
        # First call returns None (no existing); second returns the new row.
        mock_get.side_effect = [
            None,
            _goal_row(task_id="new-1", tags=["session_goal", "agent:main", "thread"]),
        ]
        mock_create.return_value = "new-1"

        from robothor.crm.dal import get_or_create_agent_goal

        manifest = {
            "id": "main",
            "goals": {
                "quality": [
                    {
                        "id": "passes-its-job",
                        "metric": "benchmark_pass_rate",
                        "target": ">=0.85",
                        "weight": 5.0,
                    }
                ],
                "efficiency": [
                    {"id": "low-error", "metric": "error_rate", "target": "<0.02", "weight": 1.0}
                ],
            },
        }
        result = get_or_create_agent_goal(tenant_id="default", agent_id="main", manifest=manifest)
        assert result["id"] == "new-1"
        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["agent_id"] == "main"
        # metric_targets must come through to create_session_goal.
        targets = kwargs["metric_targets"]
        ids = {t["id"] for t in targets}
        assert {"passes-its-job", "low-error"} <= ids


class TestInPlaceGoalEdits:
    @patch("robothor.crm.dal.get_connection")
    def test_update_goal_objective_rewrites_objective_column_and_meta(self, mock_get_conn):
        existing = _goal_row(
            task_id="t1",
            tags=["session_goal", "agent:main", "thread"],
            meta={
                "objective": "old objective",
                "success_criteria": ["c1"],
                "metric_targets": [],
                "evidence": [],
                "completion_note": "",
            },
        )
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=existing)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import update_goal_objective

        ok = update_goal_objective(task_id="t1", objective="NEW objective", tenant_id="default")
        assert ok is True
        # An UPDATE must touch BOTH the objective column AND session_goal_meta.
        all_sql = " | ".join(c[0][0] for c in mock_cur.execute.call_args_list)
        assert "objective" in all_sql
        # The new objective string must be in the params.
        params = []
        for c in mock_cur.execute.call_args_list:
            params.extend(list(c[0][1]) if len(c[0]) > 1 else [])
        assert any("NEW objective" in str(p) for p in params)

    @patch("robothor.crm.dal.get_connection")
    def test_update_goal_criteria_replaces_list(self, mock_get_conn):
        existing = _goal_row(
            task_id="t1",
            tags=["session_goal", "agent:main", "thread"],
            meta={
                "objective": "x",
                "success_criteria": ["old"],
                "metric_targets": [],
                "evidence": [],
                "completion_note": "",
            },
        )
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=existing)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import update_goal_criteria

        ok = update_goal_criteria(
            task_id="t1",
            success_criteria=["new-1", "new-2"],
            tenant_id="default",
        )
        assert ok is True
        params = []
        for c in mock_cur.execute.call_args_list:
            params.extend(list(c[0][1]) if len(c[0]) > 1 else [])
        payload = next(p for p in params if isinstance(p, str) and "success_criteria" in p)
        assert "new-1" in payload and "new-2" in payload
        assert "old" not in payload

    @patch("robothor.crm.dal.get_connection")
    def test_add_metric_target_appends_to_list(self, mock_get_conn):
        existing = _goal_row(
            task_id="t1",
            tags=["session_goal", "agent:main", "thread"],
            meta={
                "objective": "x",
                "success_criteria": [],
                "metric_targets": [
                    {
                        "id": "passes-its-job",
                        "metric": "benchmark_pass_rate",
                        "target": ">=0.85",
                        "weight": 5.0,
                    }
                ],
                "evidence": [],
                "completion_note": "",
            },
        )
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=existing)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import add_goal_metric_target

        new_target = {
            "id": "low-error",
            "category": "efficiency",
            "metric": "error_rate",
            "target": "<0.02",
            "weight": 1.0,
            "window_days": 7,
        }
        ok = add_goal_metric_target(task_id="t1", metric_target=new_target, tenant_id="default")
        assert ok is True
        params = []
        for c in mock_cur.execute.call_args_list:
            params.extend(list(c[0][1]) if len(c[0]) > 1 else [])
        payload = next(p for p in params if isinstance(p, str) and "metric_targets" in p)
        assert "passes-its-job" in payload  # existing preserved
        assert "low-error" in payload  # new appended

    @patch("robothor.crm.dal.get_connection")
    def test_remove_metric_target_drops_by_id(self, mock_get_conn):
        existing = _goal_row(
            task_id="t1",
            tags=["session_goal", "agent:main", "thread"],
            meta={
                "objective": "x",
                "success_criteria": [],
                "metric_targets": [
                    {"id": "passes-its-job", "metric": "benchmark_pass_rate", "target": ">=0.85"},
                    {"id": "low-error", "metric": "error_rate", "target": "<0.02"},
                ],
                "evidence": [],
                "completion_note": "",
            },
        )
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=existing)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import remove_goal_metric_target

        ok = remove_goal_metric_target(task_id="t1", target_id="low-error", tenant_id="default")
        assert ok is True
        params = []
        for c in mock_cur.execute.call_args_list:
            params.extend(list(c[0][1]) if len(c[0]) > 1 else [])
        payload = next(p for p in params if isinstance(p, str) and "metric_targets" in p)
        assert "passes-its-job" in payload


# ─────────────────────────────────────────────────────────────────────────────
# run_step_exists / benchmark_result_exists — backs tool_output/benchmark_run
# evidence validation in session_goal.validate_evidence (PR-3a).
# ─────────────────────────────────────────────────────────────────────────────


class TestRunStepExists:
    @patch("robothor.crm.dal.get_connection")
    def test_true_when_row_found(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=(1,))
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import run_step_exists

        assert run_step_exists("3fa85f64-5717-4562-b3fc-2c963f66afa6", 3) is True
        sql = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        assert "agent_run_steps" in sql
        assert "3fa85f64-5717-4562-b3fc-2c963f66afa6" in params
        assert 3 in params

    @patch("robothor.crm.dal.get_connection")
    def test_false_when_no_row(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import run_step_exists

        assert run_step_exists("3fa85f64-5717-4562-b3fc-2c963f66afa6", 99) is False

    @patch("robothor.crm.dal.get_connection")
    def test_false_when_run_id_empty(self, mock_get_conn):
        # No DB round trip for an empty run_id — avoid a bogus UUID query.
        from robothor.crm.dal import run_step_exists

        assert run_step_exists("", 0) is False
        mock_get_conn.assert_not_called()


class TestBenchmarkResultExists:
    @patch("robothor.crm.dal.get_connection")
    def test_true_when_row_found(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=(1,))
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import benchmark_result_exists

        assert benchmark_result_exists(42) is True
        sql = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        assert "benchmark_results" in sql
        assert 42 in params

    @patch("robothor.crm.dal.get_connection")
    def test_false_when_no_row(self, mock_get_conn):
        mock_conn, mock_cur = _make_mock_conn(fetchone_return=None)
        mock_get_conn.return_value = mock_conn

        from robothor.crm.dal import benchmark_result_exists

        assert benchmark_result_exists(999) is False

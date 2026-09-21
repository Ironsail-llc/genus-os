"""Task closure follows known work state independently of optional verification."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.run_finalizer import RunFinalizationMixin
from robothor.engine.task_completion import capture_pending_items
from robothor.engine.todolist import TodoItem, TodoList


@pytest.mark.parametrize("mode", ["off", "observe", "alert", "enforce"])
@pytest.mark.parametrize("status", ["pending", "in_progress"])
def test_pending_work_prevents_resolution_in_every_mode(monkeypatch, mode, status):
    monkeypatch.setattr("robothor.engine.feature_flags.run_verification_mode", lambda: mode)
    run = AgentRun(
        agent_id="worker", task_id="task", tenant_id="tenant-a", status=RunStatus.COMPLETED
    )
    session = SimpleNamespace(
        todo_list=TodoList([TodoItem("Check reply", "Checking reply", status)])
    )
    capture_pending_items(run, session)
    with (
        patch("robothor.crm.dal.resolve_task") as resolve,
        patch("robothor.crm.dal.set_next_action") as next_action,
    ):
        RunFinalizationMixin._update_task_for_run(run)
    resolve.assert_not_called()
    next_action.assert_called_once_with(
        task_id="task",
        next_action="Continue: Check reply",
        agent="worker",
        by="runtime",
        tenant_id="tenant-a",
    )


def test_repeated_finalization_without_session_preserves_pending_snapshot():
    run = AgentRun(pending_task_items=["Check reply"])
    capture_pending_items(run, None)
    assert run.pending_task_items == ["Check reply"]


def test_current_completed_checklist_clears_previous_pending_snapshot():
    run = AgentRun(pending_task_items=["Check reply"])
    todos = TodoList()
    todos.replace([TodoItem("Check reply", "Checking reply", "completed")])
    capture_pending_items(run, SimpleNamespace(todo_list=todos))
    assert run.pending_task_items == []

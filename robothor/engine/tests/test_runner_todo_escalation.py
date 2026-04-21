"""Stage 5 — on run end, lift unfinished todo_write items back to the CRM
parent task as next_action so the planner picks up the thread next beat.

Closes the "full circle" gap: a worker that runs out of iterations/budget
with items still pending must not silently drop them. If the run had a
parent_task_id, the remaining work propagates to the thread pool.
"""

from __future__ import annotations

from unittest.mock import patch

from robothor.engine.todolist import TodoItem, TodoList


def _todo_list(items_spec: list[tuple[str, str]]) -> TodoList:
    """items_spec: list of (content, status) tuples."""
    return TodoList(
        items=[
            TodoItem(content=content, active_form=f"Doing {content}", status=status)
            for content, status in items_spec
        ]
    )


class TestEscalateUnfinishedTodos:
    def test_unfinished_items_write_next_action_on_parent(self):
        from robothor.engine.runner import _escalate_unfinished_todos

        todos = _todo_list(
            [
                ("Research vendor DrFirst", "completed"),
                ("Email vendor for written pricing", "in_progress"),
                ("Compare with DoseSpot", "pending"),
            ]
        )
        parent = {
            "id": "parent-1",
            "title": "Evaluate eRx vendors",
            "tags": ["thread"],
            "objective": "Pick a vendor for RxHistory",
        }
        with (
            patch("robothor.crm.dal.get_task", return_value=parent) as gt,
            patch("robothor.crm.dal.set_next_action", return_value=True) as sna,
            patch("robothor.crm.dal.update_task", return_value=True) as ut,
        ):
            result = _escalate_unfinished_todos(
                todos=todos,
                parent_task_id="parent-1",
                agent_id="auto-researcher",
                tenant_id="default",
            )

        assert result is True
        gt.assert_called_once()
        sna.assert_called_once()
        kwargs = sna.call_args.kwargs
        assert kwargs["task_id"] == "parent-1"
        # First unfinished item becomes the next_action
        assert "Email vendor for written pricing" in kwargs["next_action"]
        assert kwargs["agent"] == "auto-researcher"
        # parent already tagged thread → no tags update
        ut.assert_not_called()

    def test_untagged_parent_becomes_thread_on_escalation(self):
        from robothor.engine.runner import _escalate_unfinished_todos

        todos = _todo_list([("Step 1", "pending"), ("Step 2", "pending")])
        parent = {
            "id": "parent-2",
            "title": "Short-running task that got stuck",
            "tags": ["urgent"],
            "objective": None,
        }
        with (
            patch("robothor.crm.dal.get_task", return_value=parent),
            patch("robothor.crm.dal.set_next_action", return_value=True),
            patch("robothor.crm.dal.update_task", return_value=True) as ut,
        ):
            _escalate_unfinished_todos(
                todos=todos,
                parent_task_id="parent-2",
                agent_id="worker",
                tenant_id="default",
            )

        ut.assert_called_once()
        kwargs = ut.call_args.kwargs
        assert kwargs["task_id"] == "parent-2"
        tags = kwargs.get("tags")
        assert tags is not None
        assert "thread" in tags
        assert "urgent" in tags  # preserve existing tags
        # Objective was empty → seed from title
        assert kwargs.get("objective") == "Short-running task that got stuck"

    def test_no_parent_task_id_is_noop(self):
        from robothor.engine.runner import _escalate_unfinished_todos

        todos = _todo_list([("A", "pending"), ("B", "pending")])
        with (
            patch("robothor.crm.dal.get_task") as gt,
            patch("robothor.crm.dal.set_next_action") as sna,
            patch("robothor.crm.dal.update_task") as ut,
        ):
            result = _escalate_unfinished_todos(
                todos=todos,
                parent_task_id=None,
                agent_id="worker",
                tenant_id="default",
            )
        assert result is False
        gt.assert_not_called()
        sna.assert_not_called()
        ut.assert_not_called()

    def test_all_completed_is_noop(self):
        from robothor.engine.runner import _escalate_unfinished_todos

        todos = _todo_list([("A", "completed"), ("B", "completed")])
        with (
            patch("robothor.crm.dal.get_task") as gt,
            patch("robothor.crm.dal.set_next_action") as sna,
            patch("robothor.crm.dal.update_task") as ut,
        ):
            result = _escalate_unfinished_todos(
                todos=todos,
                parent_task_id="parent-3",
                agent_id="worker",
                tenant_id="default",
            )
        assert result is False
        gt.assert_not_called()
        sna.assert_not_called()
        ut.assert_not_called()

    def test_empty_todo_list_is_noop(self):
        from robothor.engine.runner import _escalate_unfinished_todos

        assert (
            _escalate_unfinished_todos(
                todos=None,
                parent_task_id="parent-4",
                agent_id="worker",
                tenant_id="default",
            )
            is False
        )
        assert (
            _escalate_unfinished_todos(
                todos=TodoList(items=[]),
                parent_task_id="parent-4",
                agent_id="worker",
                tenant_id="default",
            )
            is False
        )

    def test_parent_not_found_is_noop(self):
        from robothor.engine.runner import _escalate_unfinished_todos

        todos = _todo_list([("X", "pending")])
        with (
            patch("robothor.crm.dal.get_task", return_value=None),
            patch("robothor.crm.dal.set_next_action") as sna,
            patch("robothor.crm.dal.update_task") as ut,
        ):
            result = _escalate_unfinished_todos(
                todos=todos,
                parent_task_id="missing",
                agent_id="worker",
                tenant_id="default",
            )
        assert result is False
        sna.assert_not_called()
        ut.assert_not_called()

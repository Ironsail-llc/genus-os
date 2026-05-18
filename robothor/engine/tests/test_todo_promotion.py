"""Phase 3 — promote unfinished `todo_write` items to CRM subtasks.

When a worker exits with `pending` / `in_progress` items still on its TodoList
AND its agent manifest opts in, we want the unfinished items to surface as
real CRM subtasks under the parent thread — not just live as a single
`next_action` string. That way the Helm task board shows the queue, the
thread planner has discrete units of work to re-plan, and stalled items are
visible to the operator instead of being silently buried in a free-text
hint.

Phase 3 is purely additive: the existing `_escalate_unfinished_todos` flow
still writes the `next_action` hint. Promotion piles on top of that with a
double-gated safety model (env kill switch + manifest opt-in), content-hash
idempotency, and a one-level tag-based cycle guard.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any
from unittest.mock import MagicMock, patch


def _make_item(content: str = "do the thing", status: str = "pending") -> Any:
    """Build a minimal TodoItem stand-in for tests."""
    from robothor.engine.todolist import TodoItem

    return TodoItem(content=content, active_form=f"Doing {content}", status=status)


def _make_parent(
    *,
    task_id: str = "parent-1",
    tags: list[str] | None = None,
    priority: str = "normal",
    assigned_to_agent: str = "main",
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": "Parent thread",
        "tags": tags if tags is not None else ["thread"],
        "priority": priority,
        "assigned_to_agent": assigned_to_agent,
    }


class TestComputeItemHash:
    """The hash is the idempotency key. Same parent + content → same hash."""

    def test_hash_is_sixteen_chars(self):
        from robothor.engine.todo_promotion import compute_item_hash

        h = compute_item_hash("parent-uuid-1234", "Email April for the quote")
        assert isinstance(h, str)
        assert len(h) == 16

    def test_hash_is_deterministic(self):
        from robothor.engine.todo_promotion import compute_item_hash

        a = compute_item_hash("parent-uuid-1234", "Email April for the quote")
        b = compute_item_hash("parent-uuid-1234", "Email April for the quote")
        assert a == b

    def test_hash_normalizes_whitespace_and_case(self):
        """Spurious whitespace + case shouldn't create a duplicate subtask."""
        from robothor.engine.todo_promotion import compute_item_hash

        a = compute_item_hash("parent-1", "Email April")
        b = compute_item_hash("parent-1", "  email april  ")
        assert a == b

    def test_hash_differs_across_parents(self):
        """Same item wording under different parents must hash differently."""
        from robothor.engine.todo_promotion import compute_item_hash

        a = compute_item_hash("parent-A", "Email April")
        b = compute_item_hash("parent-B", "Email April")
        assert a != b

    def test_hash_matches_sha256_first_16(self):
        from robothor.engine.todo_promotion import compute_item_hash

        h = compute_item_hash("p", "x")
        expected = hashlib.sha256(b"p:x").hexdigest()[:16]
        assert h == expected


class TestShouldPromote:
    """Cycle prevention: a subtask spawned via promotion can't promote further."""

    def test_promotes_when_parent_lacks_promoted_todo_tag(self):
        from robothor.engine.todo_promotion import should_promote

        assert should_promote(_make_parent(tags=["thread"])) is True

    def test_skips_when_parent_already_carries_promoted_todo_tag(self):
        from robothor.engine.todo_promotion import should_promote

        # This parent was itself promoted — don't recurse.
        assert should_promote(_make_parent(tags=["thread", "promoted_todo"])) is False

    def test_handles_missing_tags(self):
        from robothor.engine.todo_promotion import should_promote

        # Missing tags is the same as no tags — safe to promote.
        assert should_promote({"id": "p", "title": "x"}) is True


class TestPromoteTodoToSubtask:
    """The end-to-end promotion: idempotent, inherits parent agent, writes history."""

    @patch("robothor.engine.todo_promotion.dal")
    def test_creates_subtask_with_hash_in_body(self, mock_dal):
        from robothor.engine.todo_promotion import promote_todo_to_subtask

        mock_dal.find_task_by_dedup_key.return_value = None
        mock_dal.create_task.return_value = "subtask-1"

        promote_todo_to_subtask(
            parent=_make_parent(),
            item=_make_item("Email April for the quote"),
            agent_id="worker",
            run_id="run-1",
            tenant_id="default",
        )

        create_kwargs = mock_dal.create_task.call_args.kwargs
        assert "todo_hash:" in create_kwargs.get("body", "")
        assert create_kwargs["parent_task_id"] == "parent-1"
        assert "promoted_todo" in create_kwargs.get("tags", [])
        assert create_kwargs["status"] == "TODO"

    @patch("robothor.engine.todo_promotion.dal")
    def test_inherits_assigned_agent_from_parent(self, mock_dal):
        from robothor.engine.todo_promotion import promote_todo_to_subtask

        mock_dal.find_task_by_dedup_key.return_value = None
        mock_dal.create_task.return_value = "subtask-2"

        promote_todo_to_subtask(
            parent=_make_parent(assigned_to_agent="email-responder"),
            item=_make_item("chase pricing"),
            agent_id="some-worker",
            run_id="run-1",
            tenant_id="default",
        )

        assert mock_dal.create_task.call_args.kwargs["assigned_to_agent"] == "email-responder"

    @patch("robothor.engine.todo_promotion.dal")
    def test_inherits_priority_from_parent(self, mock_dal):
        from robothor.engine.todo_promotion import promote_todo_to_subtask

        mock_dal.find_task_by_dedup_key.return_value = None
        mock_dal.create_task.return_value = "subtask-3"

        promote_todo_to_subtask(
            parent=_make_parent(priority="urgent"),
            item=_make_item("urgent thing"),
            agent_id="worker",
            run_id="run-1",
            tenant_id="default",
        )

        assert mock_dal.create_task.call_args.kwargs["priority"] == "urgent"

    @patch("robothor.engine.todo_promotion.dal")
    def test_is_idempotent_when_existing_subtask_found(self, mock_dal):
        """Second promotion of the same hash returns the existing subtask id, no insert."""
        from robothor.engine.todo_promotion import promote_todo_to_subtask

        mock_dal.find_task_by_dedup_key.return_value = {"id": "existing-sub", "status": "TODO"}

        result = promote_todo_to_subtask(
            parent=_make_parent(),
            item=_make_item("Email April"),
            agent_id="worker",
            run_id="run-1",
            tenant_id="default",
        )

        assert result == "existing-sub"
        mock_dal.create_task.assert_not_called()

    @patch("robothor.engine.todo_promotion.dal")
    def test_writes_history_kind_todo_promoted(self, mock_dal):
        """Audit row carries metadata.kind="todo_promoted" so observability + the
        Phase-1 CHECK constraint accept it."""
        from robothor.engine.todo_promotion import promote_todo_to_subtask

        mock_dal.find_task_by_dedup_key.return_value = None
        mock_dal.create_task.return_value = "subtask-4"
        mock_dal.append_task_history.return_value = True

        promote_todo_to_subtask(
            parent=_make_parent(),
            item=_make_item("chase"),
            agent_id="worker",
            run_id="run-77",
            tenant_id="default",
        )

        assert mock_dal.append_task_history.called
        record_kwargs = mock_dal.append_task_history.call_args.kwargs
        metadata = record_kwargs.get("metadata", {})
        assert metadata.get("kind") == "todo_promoted"
        assert metadata.get("from_run_id") == "run-77"
        assert "content_hash" in metadata
        assert record_kwargs.get("task_id") == "subtask-4"

    @patch("robothor.engine.todo_promotion.dal")
    def test_skips_completed_items(self, mock_dal):
        """Only `pending` / `in_progress` items get promoted — completed ones are done."""
        from robothor.engine.todo_promotion import promote_todo_to_subtask

        result = promote_todo_to_subtask(
            parent=_make_parent(),
            item=_make_item("did the thing", status="completed"),
            agent_id="worker",
            run_id="run-1",
            tenant_id="default",
        )

        assert result is None
        mock_dal.create_task.assert_not_called()

    @patch("robothor.engine.todo_promotion.dal")
    def test_returns_none_when_dal_returns_error_dict(self, mock_dal):
        """If create_task fails validation (Phase-1 error path), promotion returns None."""
        from robothor.engine.todo_promotion import promote_todo_to_subtask

        mock_dal.find_task_by_dedup_key.return_value = None
        mock_dal.create_task.return_value = {"error": "bad budget"}

        result = promote_todo_to_subtask(
            parent=_make_parent(),
            item=_make_item("x"),
            agent_id="worker",
            run_id="run-1",
            tenant_id="default",
        )

        assert result is None


class TestPromoteAllForRun:
    """The runner-facing entry point: promote a batch of items with caps + guards."""

    @patch("robothor.engine.todo_promotion.dal")
    def test_promote_disabled_when_env_off(self, mock_dal):
        from robothor.engine.todo_promotion import promote_unfinished_items

        agent_config = MagicMock(todo_list_enabled=True, task_protocol=True)
        items = [_make_item("a"), _make_item("b")]

        os.environ["ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED"] = "0"
        try:
            created = promote_unfinished_items(
                parent=_make_parent(),
                items=items,
                agent_config=agent_config,
                agent_id="worker",
                run_id="run-1",
                tenant_id="default",
            )
        finally:
            os.environ.pop("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", None)

        assert created == []
        mock_dal.create_task.assert_not_called()

    @patch("robothor.engine.todo_promotion.dal")
    def test_promote_disabled_when_manifest_opt_out(self, mock_dal):
        from robothor.engine.todo_promotion import promote_unfinished_items

        agent_config = MagicMock(todo_list_enabled=False, task_protocol=True)

        os.environ["ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED"] = "1"
        try:
            created = promote_unfinished_items(
                parent=_make_parent(),
                items=[_make_item("x")],
                agent_config=agent_config,
                agent_id="worker",
                run_id="run-1",
                tenant_id="default",
            )
        finally:
            os.environ.pop("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", None)

        assert created == []
        mock_dal.create_task.assert_not_called()

    @patch("robothor.engine.todo_promotion.dal")
    def test_promote_disabled_when_task_protocol_false(self, mock_dal):
        from robothor.engine.todo_promotion import promote_unfinished_items

        agent_config = MagicMock(todo_list_enabled=True, task_protocol=False)

        os.environ["ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED"] = "1"
        try:
            created = promote_unfinished_items(
                parent=_make_parent(),
                items=[_make_item("x")],
                agent_config=agent_config,
                agent_id="worker",
                run_id="run-1",
                tenant_id="default",
            )
        finally:
            os.environ.pop("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", None)

        assert created == []
        mock_dal.create_task.assert_not_called()

    @patch("robothor.engine.todo_promotion.dal")
    def test_promote_skipped_on_promoted_parent_cycle_guard(self, mock_dal):
        from robothor.engine.todo_promotion import promote_unfinished_items

        agent_config = MagicMock(todo_list_enabled=True, task_protocol=True)
        parent = _make_parent(tags=["thread", "promoted_todo"])

        os.environ["ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED"] = "1"
        try:
            created = promote_unfinished_items(
                parent=parent,
                items=[_make_item("x")],
                agent_config=agent_config,
                agent_id="worker",
                run_id="run-1",
                tenant_id="default",
            )
        finally:
            os.environ.pop("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", None)

        assert created == []
        mock_dal.create_task.assert_not_called()

    @patch("robothor.engine.todo_promotion.dal")
    def test_promote_strips_completed_items(self, mock_dal):
        from robothor.engine.todo_promotion import promote_unfinished_items

        mock_dal.find_task_by_dedup_key.return_value = None
        mock_dal.create_task.side_effect = ["sub-a", "sub-b"]

        agent_config = MagicMock(todo_list_enabled=True, task_protocol=True)
        items = [
            _make_item("a", status="pending"),
            _make_item("done already", status="completed"),
            _make_item("b", status="in_progress"),
        ]

        os.environ["ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED"] = "1"
        try:
            created = promote_unfinished_items(
                parent=_make_parent(),
                items=items,
                agent_config=agent_config,
                agent_id="worker",
                run_id="run-1",
                tenant_id="default",
            )
        finally:
            os.environ.pop("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", None)

        # Only the two non-completed items become subtasks
        assert len(created) == 2
        assert mock_dal.create_task.call_count == 2

    @patch("robothor.engine.todo_promotion.dal")
    def test_promote_caps_at_max_per_run(self, mock_dal):
        """Don't spam a parent with 20 subtasks per run."""
        from robothor.engine.todo_promotion import MAX_PROMOTIONS_PER_RUN, promote_unfinished_items

        mock_dal.find_task_by_dedup_key.return_value = None
        mock_dal.create_task.side_effect = [f"sub-{i}" for i in range(20)]

        agent_config = MagicMock(todo_list_enabled=True, task_protocol=True)
        # Construct twice the cap of items.
        items = [_make_item(f"item-{i}") for i in range(MAX_PROMOTIONS_PER_RUN * 2)]

        os.environ["ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED"] = "1"
        try:
            created = promote_unfinished_items(
                parent=_make_parent(),
                items=items,
                agent_config=agent_config,
                agent_id="worker",
                run_id="run-1",
                tenant_id="default",
            )
        finally:
            os.environ.pop("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", None)

        assert len(created) == MAX_PROMOTIONS_PER_RUN
        assert mock_dal.create_task.call_count == MAX_PROMOTIONS_PER_RUN

    @patch("robothor.engine.todo_promotion.dal")
    def test_promote_continues_on_per_item_failure(self, mock_dal):
        """One bad subtask creation shouldn't block the others."""
        from robothor.engine.todo_promotion import promote_unfinished_items

        mock_dal.find_task_by_dedup_key.return_value = None
        mock_dal.create_task.side_effect = [
            {"error": "validation failure"},
            "sub-b",
        ]

        agent_config = MagicMock(todo_list_enabled=True, task_protocol=True)

        os.environ["ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED"] = "1"
        try:
            created = promote_unfinished_items(
                parent=_make_parent(),
                items=[_make_item("a"), _make_item("b")],
                agent_config=agent_config,
                agent_id="worker",
                run_id="run-1",
                tenant_id="default",
            )
        finally:
            os.environ.pop("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", None)

        # Only the second item succeeded; the first returned error
        assert created == ["sub-b"]

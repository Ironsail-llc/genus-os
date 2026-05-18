"""Phase 5 — TodoList survives the checkpoint round-trip.

Pre-Phase-5 the runner silently dropped session.todo_list on resume — the
`load_latest` path reconstructed messages + scratchpad + plan but not the
in-conversation checklist. Phase 5 embeds the TodoList under
``scratchpad._todo_list`` (no migration; JSONB shape change) and rebuilds
it via TodoList.from_dict / TodoItem.from_dict on resume.

These tests are CHECKPOINT-only (no runner involvement) so they don't
import litellm and run in CI without external deps. The full lifecycle
integration test lives in tests/integration/test_task_lifecycle.py.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


class TestCheckpointSaveEmbedsTodoList:
    """checkpoint.save(todo_list=…) should put the dict under scratchpad._todo_list."""

    @patch("robothor.engine.checkpoint.get_connection", create=True)
    @patch("robothor.db.connection.get_connection")
    def test_save_with_todo_list_only(self, mock_get_conn, _unused):
        from robothor.engine.checkpoint import CheckpointManager

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        cp = CheckpointManager(run_id="run-1")
        ok = cp.save(
            step_number=10,
            messages=[{"role": "user", "content": "hi"}],
            scratchpad=None,
            plan=None,
            todo_list={"items": [{"content": "a", "active_form": "Aing", "status": "pending"}]},
        )

        assert ok is True
        # 4th positional arg of the cursor.execute call is the scratchpad JSON
        call = mock_cur.execute.call_args
        params = call[0][1]
        scratchpad_json = params[3]
        decoded = json.loads(scratchpad_json)
        assert decoded["_todo_list"]["items"][0]["content"] == "a"

    @patch("robothor.db.connection.get_connection")
    def test_save_merges_todo_list_into_existing_scratchpad(self, mock_get_conn):
        """When both scratchpad and todo_list are passed, todo_list nests under it."""
        from robothor.engine.checkpoint import CheckpointManager

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        cp = CheckpointManager(run_id="run-1")
        cp.save(
            step_number=5,
            messages=[],
            scratchpad={"tool_calls": 3, "successes": 2},
            plan=None,
            todo_list={"items": [{"content": "x", "active_form": "Xing", "status": "in_progress"}]},
        )

        params = mock_cur.execute.call_args[0][1]
        decoded = json.loads(params[3])
        assert decoded["tool_calls"] == 3
        assert decoded["successes"] == 2
        assert decoded["_todo_list"]["items"][0]["content"] == "x"

    @patch("robothor.db.connection.get_connection")
    def test_save_without_todo_list_keeps_scratchpad_unmolested(self, mock_get_conn):
        from robothor.engine.checkpoint import CheckpointManager

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        cp = CheckpointManager(run_id="run-1")
        cp.save(
            step_number=5,
            messages=[],
            scratchpad={"tool_calls": 3},
            plan=None,
        )

        params = mock_cur.execute.call_args[0][1]
        decoded = json.loads(params[3])
        assert "_todo_list" not in decoded


class TestTodoListRoundTrip:
    """The TodoList.to_dict / TodoItem.from_dict pair round-trips clean."""

    def test_roundtrip_preserves_items_and_counters(self):
        from robothor.engine.todolist import TodoItem, TodoList

        original = TodoList(
            items=[
                TodoItem(content="step a", active_form="Doing a", status="completed"),
                TodoItem(content="step b", active_form="Doing b", status="in_progress"),
                TodoItem(content="step c", active_form="Doing c", status="pending"),
            ],
        )
        original._turns_since_use = 7
        original._reminder_count = 2

        snapshot = original.to_dict()

        # Resume path uses TodoList.from_dict directly.
        rebuilt = TodoList.from_dict(snapshot)

        assert len(rebuilt.items) == 3
        assert rebuilt.items[0].content == "step a"
        assert rebuilt.items[1].status == "in_progress"
        assert rebuilt._turns_since_use == 7
        assert rebuilt._reminder_count == 2

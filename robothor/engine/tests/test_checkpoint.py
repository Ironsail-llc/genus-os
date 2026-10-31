"""Tests for checkpointing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from robothor.engine.checkpoint import CHECKPOINT_INTERVAL, CheckpointManager


class TestCheckpointManager:
    def test_should_checkpoint_at_interval(self):
        mgr = CheckpointManager(run_id="test", interval=5)
        for _ in range(4):
            mgr.record_success()
            assert not mgr.should_checkpoint()
        mgr.record_success()
        assert mgr.should_checkpoint()

    def test_should_not_checkpoint_at_zero(self):
        mgr = CheckpointManager(run_id="test")
        assert not mgr.should_checkpoint()

    def test_default_interval(self):
        mgr = CheckpointManager()
        assert mgr.interval == CHECKPOINT_INTERVAL

    def test_save_best_effort(self):
        """save() doesn't raise on DB failure."""
        mgr = CheckpointManager(run_id="test")
        with patch("robothor.db.connection.get_connection", side_effect=Exception("no db")):
            result = mgr.save(1, [{"role": "user", "content": "hi"}])
        assert result is False

    def test_save_success(self):
        """save() returns True when DB write succeeds."""
        mgr = CheckpointManager(run_id="test")
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = MagicMock()

        with patch("robothor.db.connection.get_connection", return_value=mock_conn):
            result = mgr.save(5, [{"role": "user", "content": "test"}], {"tool_calls": 3})
        assert result is True
        assert mgr._checkpoint_count == 1

    def test_load_latest_returns_none_on_failure(self):
        """load_latest() returns None when DB is unavailable."""
        with patch("robothor.db.connection.get_connection", side_effect=Exception("no db")):
            result = CheckpointManager.load_latest("nonexistent")
        assert result is None


class TestCheckpointImages:
    """A turn's image blocks are not copied into every checkpoint row."""

    def _saved_messages(self, messages):
        import json

        mgr = CheckpointManager(run_id="test")
        mgr.record_success()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        cursor = MagicMock()
        mock_conn.cursor.return_value = cursor
        with patch("robothor.db.connection.get_connection", return_value=mock_conn):
            assert mgr.save(1, messages) is True
        return json.loads(cursor.execute.call_args.args[1][2])

    def test_image_blocks_become_a_short_placeholder(self):
        data = "A" * 200_000
        turn = {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this\n   path: /inbox/shot.png"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
            ],
        }
        saved = self._saved_messages([{"role": "system", "content": "sys"}, turn])
        assert data not in str(saved)
        content = saved[1]["content"]
        assert content[0] == turn["content"][0]
        assert content[1]["type"] == "text"
        assert "image" in content[1]["text"]
        assert len(str(saved)) < 1_000

    def test_the_callers_messages_are_not_changed(self):
        block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        turn = {"role": "user", "content": [{"type": "text", "text": "x"}, block]}
        self._saved_messages([turn])
        assert turn["content"][1] is block

    def test_text_only_messages_are_stored_as_they_are(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        assert self._saved_messages(msgs) == msgs

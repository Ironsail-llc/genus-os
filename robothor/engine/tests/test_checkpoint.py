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

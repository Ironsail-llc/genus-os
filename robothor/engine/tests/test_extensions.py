"""Tests for the ExtensionWatcher class in robothor/engine/extensions.py."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.extensions import ExtensionWatcher


class TestSnapshotMtimes:
    """Tests for _snapshot_mtimes."""

    def test_snapshot_mtimes(self, tmp_path: Path) -> None:
        """Create temp dir with 2 YAML files, verify returns {path: mtime} dict."""
        f1 = tmp_path / "adapter1.yaml"
        f2 = tmp_path / "adapter2.yaml"
        f1.write_text("name: a")
        f2.write_text("name: b")

        watcher = ExtensionWatcher(adapter_dir=tmp_path)
        result = watcher._snapshot_mtimes()

        assert len(result) == 2
        assert str(f1) in result
        assert str(f2) in result
        # mtimes should be floats
        for mtime in result.values():
            assert isinstance(mtime, float)

    def test_snapshot_mtimes_empty_dir(self, tmp_path: Path) -> None:
        """Empty dir returns empty dict."""
        watcher = ExtensionWatcher(adapter_dir=tmp_path)
        result = watcher._snapshot_mtimes()
        assert result == {}

    def test_snapshot_mtimes_nonexistent_dir(self) -> None:
        """Nonexistent dir returns empty dict."""
        watcher = ExtensionWatcher(adapter_dir=Path("/nonexistent/dir/abc123"))
        result = watcher._snapshot_mtimes()
        assert result == {}


class TestDetectChanges:
    """Tests for _detect_changes."""

    def test_detect_changes_added(self, tmp_path: Path) -> None:
        """Set initial mtimes to empty, add files, verify 'added' detected."""
        watcher = ExtensionWatcher(adapter_dir=tmp_path)
        watcher._file_mtimes = {}

        # Create a file after setting empty baseline
        f1 = tmp_path / "new.yaml"
        f1.write_text("name: new")

        changes = watcher._detect_changes()
        assert str(f1) in changes
        assert changes[str(f1)] == "added"

    def test_detect_changes_modified(self, tmp_path: Path) -> None:
        """Set initial mtimes, change one mtime, verify 'modified'."""
        f1 = tmp_path / "mod.yaml"
        f1.write_text("name: original")

        watcher = ExtensionWatcher(adapter_dir=tmp_path)
        # Capture initial state
        watcher._file_mtimes = watcher._snapshot_mtimes()

        # Modify file with a different mtime
        time.sleep(0.05)
        f1.write_text("name: modified")

        changes = watcher._detect_changes()
        assert str(f1) in changes
        assert changes[str(f1)] == "modified"

    def test_detect_changes_removed(self, tmp_path: Path) -> None:
        """Set initial mtimes with a file, remove it, verify 'removed'."""
        f1 = tmp_path / "gone.yaml"
        f1.write_text("name: gone")

        watcher = ExtensionWatcher(adapter_dir=tmp_path)
        watcher._file_mtimes = watcher._snapshot_mtimes()

        # Remove the file
        f1.unlink()

        changes = watcher._detect_changes()
        assert str(f1) in changes
        assert changes[str(f1)] == "removed"

    def test_detect_no_changes(self, tmp_path: Path) -> None:
        """Same mtimes twice returns empty dict."""
        f1 = tmp_path / "stable.yaml"
        f1.write_text("name: stable")

        watcher = ExtensionWatcher(adapter_dir=tmp_path)
        watcher._file_mtimes = watcher._snapshot_mtimes()

        changes = watcher._detect_changes()
        assert changes == {}


class TestGetStatus:
    """Tests for get_status."""

    def test_get_status(self) -> None:
        """Mock get_loaded_adapters, verify status dict has all fields."""
        mock_adapter = MagicMock()
        mock_adapter.name = "test-adapter"
        mock_adapter.transport = "http"
        mock_adapter.version = "1.0"
        mock_adapter.author = "test"
        mock_adapter.description = "A test adapter"
        mock_adapter.agents = ["main"]

        watcher = ExtensionWatcher()
        watcher._running = True

        with patch("robothor.engine.extensions.get_loaded_adapters", return_value=[mock_adapter]):
            status = watcher.get_status()

        assert "adapter_dir" in status
        assert status["adapter_count"] == 1
        assert status["watching"] is True
        assert status["poll_interval"] == 10
        assert len(status["adapters"]) == 1
        assert status["adapters"][0]["name"] == "test-adapter"
        assert status["adapters"][0]["transport"] == "http"
        assert status["adapters"][0]["version"] == "1.0"


class TestWatchCancellation:
    """Tests for the watch() async loop."""

    @pytest.mark.asyncio
    async def test_watch_cancellation(self, tmp_path: Path) -> None:
        """Start watch(), cancel immediately, verify exits cleanly."""
        watcher = ExtensionWatcher(adapter_dir=tmp_path, poll_interval=1)

        with patch("robothor.engine.extensions.refresh_adapters", return_value=[]):
            task = asyncio.create_task(watcher.watch())
            # Give it a moment to start
            await asyncio.sleep(0.05)
            assert watcher._running is True

            task.cancel()
            # The watch() method catches CancelledError internally and returns
            with contextlib.suppress(asyncio.CancelledError):
                await task

            assert watcher._running is False

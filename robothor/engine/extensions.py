"""Extension hot-reload — watches the adapters directory for changes.

Monitors ~/.config/robothor/adapters/ for YAML file additions, modifications,
and deletions. When changes are detected, reloads adapter configs and updates
the ToolRegistry without requiring a daemon restart.

Built on the existing adapters.py system — this adds lifecycle management,
not a parallel framework.

Usage (from daemon.py):
    from robothor.engine.extensions import ExtensionWatcher
    watcher = ExtensionWatcher()
    asyncio.create_task(watcher.watch())
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from robothor.engine.adapters import ADAPTER_DIR, get_loaded_adapters, refresh_adapters

logger = logging.getLogger(__name__)


class ExtensionWatcher:
    """Watches the adapter directory for changes and triggers hot-reload.

    Uses a simple polling approach (check mtime every 10s) instead of
    inotify/watchdog to avoid an external dependency. The adapter directory
    is small (typically 0-5 files), so polling is negligible overhead.
    """

    def __init__(self, adapter_dir: Path | None = None, poll_interval: int = 10) -> None:
        self._dir = adapter_dir or ADAPTER_DIR
        self._poll_interval = poll_interval
        self._file_mtimes: dict[str, float] = {}
        self._running = False

    def _snapshot_mtimes(self) -> dict[str, float]:
        """Get current mtimes for all YAML files in the adapter directory."""
        if not self._dir.is_dir():
            return {}
        return {str(p): p.stat().st_mtime for p in self._dir.glob("*.yaml") if p.is_file()}

    def _detect_changes(self) -> dict[str, str]:
        """Compare current mtimes with cached snapshot.

        Returns:
            Dict of {filepath: change_type} where change_type is 'added', 'modified', 'removed'.
        """
        current = self._snapshot_mtimes()
        changes: dict[str, str] = {}

        for path, mtime in current.items():
            if path not in self._file_mtimes:
                changes[path] = "added"
            elif mtime != self._file_mtimes[path]:
                changes[path] = "modified"

        for path in self._file_mtimes:
            if path not in current:
                changes[path] = "removed"

        self._file_mtimes = current
        return changes

    async def watch(self) -> None:
        """Main watch loop — polls for changes and triggers reload."""
        self._running = True
        # Initial load
        self._file_mtimes = self._snapshot_mtimes()
        adapters = refresh_adapters(self._dir)
        if adapters:
            logger.info("Extensions: loaded %d adapters on startup", len(adapters))

        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                changes = self._detect_changes()
                if not changes:
                    continue

                for path, change_type in changes.items():
                    logger.info("Extension %s: %s", change_type, Path(path).name)

                # Reload all adapters
                adapters = refresh_adapters(self._dir)
                logger.info(
                    "Extensions reloaded: %d adapters active (%d changes detected)",
                    len(adapters),
                    len(changes),
                )

            except asyncio.CancelledError:
                logger.info("Extension watcher cancelled")
                self._running = False
                return
            except Exception as e:
                logger.warning("Extension watcher error: %s", e)

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._running = False

    def get_status(self) -> dict[str, Any]:
        """Return current extension status for the management API."""
        adapters = get_loaded_adapters()
        return {
            "adapter_dir": str(self._dir),
            "adapter_count": len(adapters),
            "adapters": [
                {
                    "name": a.name,
                    "transport": a.transport,
                    "version": a.version,
                    "author": a.author,
                    "description": a.description,
                    "agents": a.agents,
                }
                for a in adapters
            ],
            "watching": self._running,
            "poll_interval": self._poll_interval,
        }

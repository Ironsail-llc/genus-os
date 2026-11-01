"""The manifest listing's scan cache.

Extracted from ``agent_manifests.py`` along a real seam: nothing here writes a
manifest, validates one or reads a request. It holds the last scan of the
manifest directory and the stat signature it was read under, and drops both
when the router (or the installer routes) say a write happened.

The dashboard asks for the listing every time a message goes to a named
agent, and every ask used to re-read and YAML-parse the whole fleet. A hit
costs one ``stat`` per file and no reads. Only the LISTING reads through it:
validation before a write still scans fresh, because a verdict about a write
must not rest on a cached view of the fleet.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_listing_lock = threading.Lock()
_listing_cache: dict[str, Any] = {"signature": None, "scan": None}


def _listing_signature(directory: Path) -> tuple[Any, ...] | None:
    """What has to change on disk for the cached scan to be stale.

    The directory's own mtime (an add, a remove, a rename), and each
    ``*.yaml`` file's name, mtime and size (an edit in place, which does not
    move the directory's mtime). ``_defaults.yaml`` matches the glob, so an
    edit to the defaults every manifest is merged with is seen too. The schema
    mode rides along: under ``enforce`` the same files can scan differently.
    ``None`` -- never cached -- when the directory cannot be stat'ed, so a
    missing directory is reported by the loader exactly as before.
    """
    from robothor.engine.manifest_schema import schema_mode

    try:
        directory_mtime = directory.stat().st_mtime_ns
        files = []
        for path in directory.glob("*.yaml"):
            stat = path.stat()
            files.append((path.name, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return None
    files.sort()
    return (str(directory), directory_mtime, len(files), tuple(files), schema_mode())


def cached_scan(directory: Path, scan: Callable[[], Any]) -> Any:
    """``scan()``, unless nothing it would read under ``directory`` has
    changed since the last one."""
    signature = _listing_signature(directory)
    with _listing_lock:
        if signature is not None and _listing_cache["signature"] == signature:
            return _listing_cache["scan"]
    result = scan()
    with _listing_lock:
        _listing_cache["signature"] = signature
        _listing_cache["scan"] = result
    return result


def invalidate_manifest_listing() -> None:
    """Drop the cached scan. Every route that writes a manifest calls this,
    so the bridge's own writes never depend on the filesystem's clock."""
    with _listing_lock:
        _listing_cache["signature"] = None
        _listing_cache["scan"] = None

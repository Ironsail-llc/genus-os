"""The agent-manifest listing's scan cache.

The dashboard asks for ``GET /api/agent-manifests`` every time a message goes
to a named agent, and every ask used to re-read and YAML-parse the whole fleet.
This holds the last scan and the stat signature it was read under; a hit costs
one ``stat`` per file and no reads. Only the LISTING reads through it:
validation before a write still scans fresh, because a verdict about a write
must not rest on a cached view of the fleet.

Its own module because it is its own concern: nothing here knows what a
manifest says, only whether the files that hold them have changed. The router
hands it the directory and the scan to run.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_listing_lock = threading.Lock()
_listing_cache: dict[str, Any] = {"signature": None, "scan": None}
#: Bumped by every invalidation. A scan stores its result only if no
#: invalidation ran while it was reading, so a write that lands mid-scan cannot
#: leave a pre-write view behind under a signature that still matches.
_generation = 0


def listing_signature(directory: Path) -> tuple[Any, ...] | None:
    """What has to change on disk for the cached scan to be stale.

    The directory's own mtime (an add, a remove, a rename), and each
    ``*.yaml`` file's name, mtime and size (an edit in place, which does not
    move the directory's mtime). ``_defaults.yaml`` matches the glob, so an
    edit to the defaults every manifest is merged with is seen too. The schema
    mode rides along: under ``enforce`` the same files can scan differently.
    So does a fingerprint of the environment: the loader expands ``${VAR}``
    from it, so the same files can also scan differently after a change to it.
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
    environment = hash(frozenset(os.environ.items()))
    return (
        str(directory),
        directory_mtime,
        len(files),
        tuple(files),
        schema_mode(),
        environment,
    )


def cached_scan(directory: Path, scan: Callable[[], Any]) -> Any:
    """``scan()``, unless nothing it would read has changed since the last one."""
    with _listing_lock:
        generation = _generation
    signature = listing_signature(directory)
    with _listing_lock:
        if signature is not None and _listing_cache["signature"] == signature:
            return _listing_cache["scan"]
    result = scan()
    with _listing_lock:
        if generation == _generation:
            _listing_cache["signature"] = signature
            _listing_cache["scan"] = result
    return result


def invalidate_manifest_listing() -> None:
    """Drop the cached scan. Every route that writes a manifest calls this,
    so the bridge's own writes never depend on the filesystem's clock."""
    global _generation
    with _listing_lock:
        _generation += 1
        _listing_cache["signature"] = None
        _listing_cache["scan"] = None

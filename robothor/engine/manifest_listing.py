"""The fleet listing's cached scan.

The dashboard asks for the fleet listing each time a message goes to a named
agent, and every ask re-read and YAML-parsed every manifest. ``listing()``
keeps the last scan and the signature it was read under; a hit costs one
``stat`` per file and no reads. Two ways it is dropped: the signature moves
(anything that changed on disk, whoever changed it), and every write path in
this process calls ``invalidate_listing()`` so its own writes never depend on
the filesystem's clock.

Its own module because it is its own concern: nothing here knows what a
manifest says, only whether the files that hold them have changed.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_listing_lock = threading.Lock()
_listing_cache: dict[str, Any] = {"signature": None, "scan": None}
#: Bumped by every invalidation. A scan stores its result only if no
#: invalidation ran while it was reading, so a write that lands mid-scan cannot
#: leave a pre-write view behind under a signature that still matches.
_listing_generation = 0


def listing_signature(directory: Path) -> tuple[Any, ...] | None:
    """What has to change for the cached scan to be stale.

    The directory's own mtime (an add, a remove, a rename), and each
    ``*.yaml`` file's name, mtime and size (an edit in place, which does not
    move the directory's mtime); ``_defaults.yaml`` matches the glob. The
    schema mode rides along (under ``enforce`` the same files scan
    differently), and so does a fingerprint of the environment the loader
    expands ``${VAR}`` from. ``None`` -- never cached -- when the directory
    cannot be stat'ed, so a missing directory is reported by the loader.
    """
    from robothor.engine import manifest_schema

    try:
        directory_mtime = directory.stat().st_mtime_ns
        files = []
        for path in directory.glob("*.yaml"):
            stat = path.stat()
            files.append((path.name, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return None
    files.sort()
    return (
        str(directory),
        directory_mtime,
        tuple(files),
        manifest_schema.schema_mode(),
        hash(frozenset(os.environ.items())),
    )


def listing() -> Any:
    """``scan()``, unless nothing it would read has changed since the last one.

    For the fleet LISTING only; anything that decides about a write calls
    ``manifest_store.scan()``.
    """
    # Lazy: manifest_store imports this module for its write paths.
    from robothor.engine import manifest_store

    directory = manifest_store.manifest_dir()
    with _listing_lock:
        generation = _listing_generation
    signature = listing_signature(directory)
    with _listing_lock:
        if signature is not None and _listing_cache["signature"] == signature:
            return _listing_cache["scan"]
    result = manifest_store.scan()
    with _listing_lock:
        if generation == _listing_generation:
            _listing_cache["signature"] = signature
            _listing_cache["scan"] = result
    return result


def invalidate_listing() -> None:
    """Drop the cached listing. Every write path in the engine calls this."""
    global _listing_generation
    with _listing_lock:
        _listing_generation += 1
        _listing_cache["signature"] = None
        _listing_cache["scan"] = None

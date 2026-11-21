"""Fan-out of room events. For now only the per-room turn lock.

One agent turn runs in a room at a time: a second sender waits on (or is told
about) the same lock. Locks are held weakly, so a room nobody is using costs
nothing; a held lock is kept alive by its holder, so every caller that arrives
while it is held gets the same one.
"""

from __future__ import annotations

import asyncio
import threading
import weakref

_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_guard = threading.Lock()


def room_lock(room_id: str) -> asyncio.Lock:
    """The lock one agent turn in ``room_id`` holds."""
    with _guard:
        lock = _locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[room_id] = lock
        return lock

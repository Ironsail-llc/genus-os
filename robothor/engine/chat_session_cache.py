"""A bounded, least-recently-used cache of chat sessions over a durable store.

Extracted from ``chat.py`` (the decomposition ratchet caps it). The cache owns
the policy; ``chat.py`` owns the session type, the store and the main key.

With per-user sessions enforced every member gets a long-lived entry that
nothing used to remove. Each session's history is capped elsewhere; the number
of sessions is capped here: creating a key past ``max_sessions`` evicts the
least-recently-used session that has no running task, no pending plan and no
deep run, and never a pinned key (the main session). An evicted key is
rehydrated from the store on its next access, so a member loses nothing but
the memory their idle session occupied. A key this process never saw is
genuinely new (startup restored everything the store had) and costs no query.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from robothor.engine.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _is_live(session: Any) -> bool:
    task = getattr(session, "active_task", None)
    if task is not None and not task.done():
        return True
    return (
        getattr(session, "active_plan", None) is not None
        or getattr(session, "active_deep", None) is not None
    )


class SessionCache:
    def __init__(
        self,
        *,
        max_sessions: int,
        idle_ttl_s: float,
        is_pinned: Callable[[str], bool],
        loader: Callable[[str], dict[str, Any]],
    ) -> None:
        self.sessions: dict[str, Any] = {}
        #: Keys this process evicted: the only ones whose next access consults the store.
        self.evicted: set[str] = set()
        self.max_sessions = max_sessions
        self.idle_ttl_s = idle_ttl_s
        self._is_pinned = is_pinned
        self._loader = loader

    def _evictable(self, key: str, session: Any) -> bool:
        return not self._is_pinned(key) and not _is_live(session)

    def _evict(self, key: str) -> None:
        del self.sessions[key]
        self.evicted.add(key)

    def _make_room(self) -> None:
        """Make room for one more session; never drops live work to do it."""
        while len(self.sessions) >= self.max_sessions:
            candidates = [
                (s.last_used, k) for k, s in self.sessions.items() if self._evictable(k, s)
            ]
            if not candidates:
                return
            self._evict(min(candidates)[1])

    def evict_idle(self, *, ttl_s: float | None = None, now: float | None = None) -> int:
        """Drop every evictable session idle for longer than *ttl_s*. Returns the count."""
        ttl = self.idle_ttl_s if ttl_s is None else ttl_s
        at = time.monotonic() if now is None else now
        stale = [
            k for k, s in self.sessions.items() if at - s.last_used > ttl and self._evictable(k, s)
        ]
        for key in stale:
            self._evict(key)
        return len(stale)

    def _rehydrate(self, key: str, factory: Callable[[], Any]) -> Any:
        """A session this process evicted comes back from the store, history and all."""
        session = factory()
        try:
            data = self._loader(key)
        except Exception as exc:  # noqa: BLE001 - a store outage costs history, not the turn
            logger.warning(
                "chat session %s: rehydration failed: %s", sanitize_log(key), sanitize_log(str(exc))
            )
            return session
        history = data.get("history") if data else None
        if history:
            session.history = list(history)
        model = data.get("model_override") if data else None
        if model:
            session.model_override = model
        return session

    def get(self, key: str, factory: Callable[[], Any]) -> Any:
        """The session for *key*, created (or rehydrated) on a miss; refreshes recency."""
        session = self.sessions.get(key)
        if session is None:
            self._make_room()
            session = self._rehydrate(key, factory) if key in self.evicted else factory()
            self.evicted.discard(key)
            self.sessions[key] = session
        session.last_used = time.monotonic()
        return session

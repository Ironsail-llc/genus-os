"""Where an ingestor left off, and what it has already published.

Two tables (migration 149):

* ``workspace_sync_state`` -- one row per (tenant, provider, mailbox,
  resource): the provider's delta link, the **high-water mark** (the newest
  item time the ingestor has processed) and when the delta was initialised.
* ``workspace_seen`` -- every external id the ingestor has handled, so the
  same message is never published twice, even after the provider throws the
  delta away and the ingestor has to start a new one.

The seen set decides what is new: in an ordinary delta round every unseen
item is published, whatever its timestamp. The high-water mark only guards a
reset: after a lost delta (Graph's 410 ``syncStateNotFound``), an item is
published only when it is not seen AND at or after the high-water mark
(strictly after, when the seen set no longer covers the mark). :class:`MemoryIngestStore` is the in-process
store the unit tests use; :class:`PgIngestStore` is the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

__all__ = [
    "SEEN_TTL",
    "IngestStore",
    "MemoryIngestStore",
    "PgIngestStore",
    "SyncState",
]

#: How long a seen id is kept. Longer than any window an ingestor re-reads
#: (a mail resync looks back a day; a calendar window spans 31 days).
SEEN_TTL = timedelta(days=45)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SyncState:
    """One resource's delta position. ``delta_link`` empty = start a new delta."""

    tenant_id: str
    provider: str
    mailbox: str
    resource: str
    delta_link: str = ""
    high_water: datetime | None = None
    initialized_at: datetime | None = None
    updated_at: datetime | None = None

    def with_(self, **changes: Any) -> SyncState:
        return replace(self, **changes)


class IngestStore(Protocol):
    async def load(
        self, tenant_id: str, provider: str, mailbox: str, resource: str
    ) -> SyncState | None: ...
    async def save(self, state: SyncState) -> None: ...
    async def seen(
        self, tenant_id: str, provider: str, external_ids: Iterable[str]
    ) -> set[str]: ...
    async def mark_seen(
        self, tenant_id: str, provider: str, external_ids: Iterable[str]
    ) -> None: ...
    async def purge_seen(self, tenant_id: str, older_than: datetime) -> int: ...


class MemoryIngestStore:
    """The store in a dict. Same semantics as the database, for tests."""

    def __init__(self, *, now: Callable[[], datetime] = _now) -> None:
        self.states: dict[tuple[str, str, str, str], SyncState] = {}
        self.seen_at: dict[tuple[str, str, str], datetime] = {}
        self._now = now

    async def load(
        self, tenant_id: str, provider: str, mailbox: str, resource: str
    ) -> SyncState | None:
        return self.states.get((tenant_id, provider, mailbox.lower(), resource))

    async def save(self, state: SyncState) -> None:
        state = state.with_(mailbox=state.mailbox.lower(), updated_at=self._now())
        self.states[(state.tenant_id, state.provider, state.mailbox, state.resource)] = state

    async def seen(self, tenant_id: str, provider: str, external_ids: Iterable[str]) -> set[str]:
        return {i for i in external_ids if (tenant_id, provider, i) in self.seen_at}

    async def mark_seen(self, tenant_id: str, provider: str, external_ids: Iterable[str]) -> None:
        for external_id in external_ids:
            self.seen_at.setdefault((tenant_id, provider, external_id), self._now())

    async def purge_seen(self, tenant_id: str, older_than: datetime) -> int:
        stale = [k for k, when in self.seen_at.items() if k[0] == tenant_id and when < older_than]
        for key in stale:
            del self.seen_at[key]
        return len(stale)


class PgIngestStore:
    """The two tables, tenant-scoped through the RLS binding and an explicit filter.

    ``connect`` (tests) is a zero-argument callable returning a context manager
    that yields a psycopg2 connection; by default the platform pool, bound to
    the tenant with :func:`robothor.db.connection.tenant_scope`.
    """

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect_override = connect

    def _connect(self, tenant_id: str) -> Any:
        if self._connect_override is not None:
            return self._connect_override()
        from robothor.db.connection import get_connection, tenant_scope

        @contextlib.contextmanager
        def _scoped() -> Any:
            with tenant_scope(tenant_id), get_connection() as conn:
                yield conn

        return _scoped()

    def _load(self, tenant_id: str, provider: str, mailbox: str, resource: str) -> SyncState | None:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT delta_link, high_water, initialized_at, updated_at "
                "FROM workspace_sync_state WHERE tenant_id = %s AND provider = %s "
                "AND mailbox = %s AND resource = %s",
                (tenant_id, provider, mailbox.lower(), resource),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return SyncState(
            tenant_id=tenant_id,
            provider=provider,
            mailbox=mailbox.lower(),
            resource=resource,
            delta_link=row[0] or "",
            high_water=row[1],
            initialized_at=row[2],
            updated_at=row[3],
        )

    def _save(self, state: SyncState) -> None:
        with self._connect(state.tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_sync_state (tenant_id, provider, mailbox, resource, "
                "delta_link, high_water, initialized_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (tenant_id, provider, mailbox, resource) DO UPDATE SET "
                "delta_link = EXCLUDED.delta_link, high_water = EXCLUDED.high_water, "
                "initialized_at = EXCLUDED.initialized_at, updated_at = now()",
                (
                    state.tenant_id,
                    state.provider,
                    state.mailbox.lower(),
                    state.resource,
                    state.delta_link,
                    state.high_water,
                    state.initialized_at,
                ),
            )
            conn.commit()

    def _seen(self, tenant_id: str, provider: str, external_ids: list[str]) -> set[str]:
        if not external_ids:
            return set()
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT external_id FROM workspace_seen WHERE tenant_id = %s AND provider = %s "
                "AND external_id = ANY(%s)",
                (tenant_id, provider, external_ids),
            )
            return {str(row[0]) for row in cur.fetchall()}

    def _mark_seen(self, tenant_id: str, provider: str, external_ids: list[str]) -> None:
        if not external_ids:
            return
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_seen (tenant_id, provider, external_id) "
                "SELECT %s, %s, unnest(%s::text[]) "
                "ON CONFLICT (tenant_id, provider, external_id) DO NOTHING",
                (tenant_id, provider, external_ids),
            )
            conn.commit()

    def _purge_seen(self, tenant_id: str, older_than: datetime) -> int:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM workspace_seen WHERE tenant_id = %s AND seen_at < %s",
                (tenant_id, older_than),
            )
            count = int(cur.rowcount or 0)
            conn.commit()
        return count

    async def load(
        self, tenant_id: str, provider: str, mailbox: str, resource: str
    ) -> SyncState | None:
        return await asyncio.to_thread(self._load, tenant_id, provider, mailbox, resource)

    async def save(self, state: SyncState) -> None:
        await asyncio.to_thread(self._save, state)

    async def seen(self, tenant_id: str, provider: str, external_ids: Iterable[str]) -> set[str]:
        return await asyncio.to_thread(self._seen, tenant_id, provider, list(external_ids))

    async def mark_seen(self, tenant_id: str, provider: str, external_ids: Iterable[str]) -> None:
        await asyncio.to_thread(self._mark_seen, tenant_id, provider, list(external_ids))

    async def purge_seen(self, tenant_id: str, older_than: datetime) -> int:
        return await asyncio.to_thread(self._purge_seen, tenant_id, older_than)

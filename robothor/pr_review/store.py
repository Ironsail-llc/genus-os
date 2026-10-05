"""Review state: the ``pr_reviews`` table and its two companions (migration 145).

* ``pr_reviews`` — one row per (tenant, repo, number): the head we know, the
  head we last reviewed, the head a task is queued for, status, Chat refs, our
  review ids and the last review's findings (for the next re-review).
* ``pr_review_messages`` — every Chat message the intake has handled, with
  what it did; a message is handled once, and a failing one is recorded and
  skipped rather than retried forever.
* ``pr_review_cursors`` — where each Chat source's last poll stopped.

**Concurrent writers.** The intake (a cron workflow) and the reviewer agent
(prepare / finalize) write the same row. :meth:`save` therefore writes only the
columns that changed since the row was read, so one writer never reverts what
the other set in between (a re-review trigger raised while a review is being
posted survives the post). State changes that must happen exactly once —
claiming a row for a review job, and claiming a finished job for posting — go
through :meth:`transition`, a compare-and-set on ``status`` (and ``job_id``).
The intake itself runs under :meth:`intake_lock`, a per-tenant advisory lock,
so two ticks never dispatch the same head twice.

:class:`MemoryStore` is the same contract in memory, for tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

__all__ = [
    "ACTIVE_STATUSES",
    "FINAL_STATUSES",
    "MemoryStore",
    "PgStore",
    "PrReviewRow",
    "PrReviewStore",
]

#: A task exists and has not been finalized. ``posting``: finalize has claimed
#: the finished job and is posting it; a retried finalize resumes from here.
ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "reviewing", "posting"})
#: A review was posted for the row's last job.
FINAL_STATUSES: frozenset[str] = frozenset({"approved", "changes_requested", "commented"})


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class PrReviewRow:
    tenant_id: str
    repo: str
    number: int
    url: str = ""
    title: str = ""
    author: str = ""
    source: str = ""
    status: str = "pending"
    head_sha: str = ""
    last_reviewed_sha: str = ""
    queued_sha: str = ""
    pending_trigger: str = ""
    trigger_text: str = ""
    mode: str = ""
    depth: str = ""
    task_id: str = ""
    chat_space: str = ""
    chat_thread: str = ""
    chat_message: str = ""
    chat_poster: str = ""
    telegram_ref: str = ""
    followup: bool = False
    review_ids: list[int] = field(default_factory=list)
    last_review: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    error: str = ""
    #: The coding job prepare started for queued_sha; finalize accepts no other.
    job_id: str = ""
    #: When the last review attempt failed (retry cooldown), or None.
    failed_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


_COLUMNS: tuple[str, ...] = tuple(
    k for k in PrReviewRow.__dataclass_fields__ if k not in ("created_at", "updated_at")
)
_JSON_COLUMNS = frozenset({"review_ids", "last_review"})
_KEY_COLUMNS = frozenset({"tenant_id", "repo", "number"})
_LOADED = "_loaded"


def _snapshot(row: PrReviewRow) -> dict[str, Any]:
    return {c: copy.deepcopy(getattr(row, c)) for c in _COLUMNS}


def _mark_loaded(row: PrReviewRow) -> PrReviewRow:
    """Remember the values as read, so :meth:`save` can write only what changed."""
    row.__dict__[_LOADED] = _snapshot(row)
    return row


def _changed(row: PrReviewRow) -> list[str] | None:
    """Columns changed since the row was read; None for a row never read (insert)."""
    loaded = row.__dict__.get(_LOADED)
    if loaded is None:
        return None
    return [c for c in _COLUMNS if c not in _KEY_COLUMNS and getattr(row, c) != loaded.get(c)]


class PrReviewStore(Protocol):
    async def get(self, tenant_id: str, repo: str, number: int) -> PrReviewRow | None: ...
    async def save(self, row: PrReviewRow) -> None: ...
    async def transition(
        self,
        tenant_id: str,
        repo: str,
        number: int,
        *,
        from_statuses: Collection[str],
        to_status: str,
        job_id: str | None = None,
        set_job_id: str | None = None,
    ) -> PrReviewRow | None: ...
    def intake_lock(self, tenant_id: str) -> contextlib.AbstractAsyncContextManager[bool]: ...
    async def list_by_thread(self, tenant_id: str, thread: str) -> list[PrReviewRow]: ...
    async def list_pending(self, tenant_id: str) -> list[PrReviewRow]: ...
    async def list_active(self, tenant_id: str) -> list[PrReviewRow]: ...
    async def list_failed(self, tenant_id: str) -> list[PrReviewRow]: ...
    async def message_seen(self, tenant_id: str, name: str) -> bool: ...
    async def record_message(
        self, tenant_id: str, name: str, kind: str, outcome: str = "", error: str = ""
    ) -> None: ...
    async def get_cursor(self, tenant_id: str, source: str) -> str: ...
    async def set_cursor(self, tenant_id: str, source: str, cursor: str) -> None: ...


class MemoryStore:
    """In-process store with the PgStore contract."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], PrReviewRow] = {}
        self.messages: dict[tuple[str, str], dict[str, str]] = {}
        self.cursors: dict[tuple[str, str], str] = {}
        self.locked: set[str] = set()

    def _copy(self, row: PrReviewRow) -> PrReviewRow:
        out = copy.deepcopy(row)
        out.__dict__.pop(_LOADED, None)
        return _mark_loaded(out)

    async def get(self, tenant_id: str, repo: str, number: int) -> PrReviewRow | None:
        row = self.rows.get((tenant_id, repo.lower(), number))
        return self._copy(row) if row else None

    async def save(self, row: PrReviewRow) -> None:
        key = (row.tenant_id, row.repo.lower(), row.number)
        changed = _changed(row)
        stored = self.rows.get(key)
        if changed is None or stored is None:
            row.updated_at = _now()
            fresh = copy.deepcopy(row)
            fresh.__dict__.pop(_LOADED, None)
            self.rows[key] = fresh
        elif changed:
            for col in changed:
                setattr(stored, col, copy.deepcopy(getattr(row, col)))
            stored.updated_at = row.updated_at = _now()
        _mark_loaded(row)

    async def transition(
        self,
        tenant_id: str,
        repo: str,
        number: int,
        *,
        from_statuses: Collection[str],
        to_status: str,
        job_id: str | None = None,
        set_job_id: str | None = None,
    ) -> PrReviewRow | None:
        stored = self.rows.get((tenant_id, repo.lower(), number))
        if stored is None or stored.status not in from_statuses:
            return None
        if job_id is not None and stored.job_id != job_id:
            return None
        stored.status = to_status
        if set_job_id is not None:
            stored.job_id = set_job_id
        stored.updated_at = _now()
        return self._copy(stored)

    @contextlib.asynccontextmanager
    async def intake_lock(self, tenant_id: str) -> AsyncIterator[bool]:
        if tenant_id in self.locked:
            yield False
            return
        self.locked.add(tenant_id)
        try:
            yield True
        finally:
            self.locked.discard(tenant_id)

    def _all(self, tenant_id: str) -> list[PrReviewRow]:
        rows = [r for (t, _, _), r in self.rows.items() if t == tenant_id]
        return [self._copy(r) for r in sorted(rows, key=lambda r: r.updated_at)]

    async def list_by_thread(self, tenant_id: str, thread: str) -> list[PrReviewRow]:
        return [r for r in self._all(tenant_id) if thread and r.chat_thread == thread]

    async def list_pending(self, tenant_id: str) -> list[PrReviewRow]:
        return [r for r in self._all(tenant_id) if r.pending_trigger]

    async def list_active(self, tenant_id: str) -> list[PrReviewRow]:
        return [r for r in self._all(tenant_id) if r.status in ACTIVE_STATUSES]

    async def list_failed(self, tenant_id: str) -> list[PrReviewRow]:
        return [r for r in self._all(tenant_id) if r.status == "failed"]

    async def message_seen(self, tenant_id: str, name: str) -> bool:
        return (tenant_id, name) in self.messages

    async def record_message(
        self, tenant_id: str, name: str, kind: str, outcome: str = "", error: str = ""
    ) -> None:
        self.messages.setdefault(
            (tenant_id, name), {"kind": kind, "outcome": outcome, "error": error}
        )

    async def get_cursor(self, tenant_id: str, source: str) -> str:
        return self.cursors.get((tenant_id, source), "")

    async def set_cursor(self, tenant_id: str, source: str, cursor: str) -> None:
        self.cursors[(tenant_id, source)] = cursor


def _row_from_db(data: dict[str, Any]) -> PrReviewRow:
    values = dict(data)
    values["review_ids"] = [int(x) for x in (values.get("review_ids") or [])]
    values["last_review"] = dict(values.get("last_review") or {})
    for key in ("url", "title", "author", "source", "head_sha", "last_reviewed_sha", "job_id"):
        values[key] = values.get(key) or ""
    known = PrReviewRow.__dataclass_fields__
    return _mark_loaded(PrReviewRow(**{k: v for k, v in values.items() if k in known}))


#: Advisory-lock namespace for the intake; hashed with the tenant id.
_INTAKE_LOCK_NS = "pr_review_intake:"


class PgStore:
    """The three tables, tenant-scoped through the RLS binding and an explicit filter."""

    def _connect(self, tenant_id: str) -> Any:
        from robothor.db.connection import get_connection, tenant_scope

        @contextlib.contextmanager
        def _scoped() -> Any:
            with tenant_scope(tenant_id), get_connection() as conn:
                yield conn

        return _scoped()

    def _select(self, tenant_id: str, where: str, params: tuple[Any, ...]) -> list[PrReviewRow]:
        from psycopg2.extras import RealDictCursor

        cols = ", ".join((*_COLUMNS, "created_at", "updated_at"))
        with self._connect(tenant_id) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT {cols} FROM pr_reviews WHERE tenant_id = %s AND {where} "
                "ORDER BY updated_at",
                (tenant_id, *params),
            )
            return [_row_from_db(dict(r)) for r in cur.fetchall()]

    @staticmethod
    def _value(row: PrReviewRow, col: str) -> Any:
        value = getattr(row, col)
        return json.dumps(value) if col in _JSON_COLUMNS else value

    def _save(self, row: PrReviewRow, changed: list[str] | None) -> None:
        with self._connect(row.tenant_id) as conn, conn.cursor() as cur:
            if changed is None:
                cols = ", ".join(_COLUMNS)
                marks = ", ".join(["%s"] * len(_COLUMNS))
                updates = ", ".join(
                    f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c not in _KEY_COLUMNS
                )
                cur.execute(
                    f"INSERT INTO pr_reviews ({cols}) VALUES ({marks}) "
                    f"ON CONFLICT (tenant_id, repo, number) DO UPDATE SET {updates}, "
                    "updated_at = now()",
                    [self._value(row, c) for c in _COLUMNS],
                )
            else:
                # Only what this writer changed: a concurrent writer's columns survive.
                sets = ", ".join(f"{c} = %s" for c in changed)
                cur.execute(
                    f"UPDATE pr_reviews SET {sets}, updated_at = now() "
                    "WHERE tenant_id = %s AND lower(repo) = lower(%s) AND number = %s",
                    [*(self._value(row, c) for c in changed), row.tenant_id, row.repo, row.number],
                )
            conn.commit()

    def _transition(
        self,
        tenant_id: str,
        repo: str,
        number: int,
        from_statuses: list[str],
        to_status: str,
        job_id: str | None,
        set_job_id: str | None,
    ) -> PrReviewRow | None:
        from psycopg2.extras import RealDictCursor

        sets: list[str] = ["status = %s", "updated_at = now()"]
        params: list[Any] = [to_status]
        if set_job_id is not None:
            sets.append("job_id = %s")
            params.append(set_job_id)
        where = "tenant_id = %s AND lower(repo) = lower(%s) AND number = %s AND status = ANY(%s)"
        params += [tenant_id, repo, number, from_statuses]
        if job_id is not None:
            where += " AND job_id = %s"
            params.append(job_id)
        cols = ", ".join((*_COLUMNS, "created_at", "updated_at"))
        with self._connect(tenant_id) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"UPDATE pr_reviews SET {', '.join(sets)} WHERE {where} RETURNING {cols}", params
            )
            found = cur.fetchone()
            conn.commit()
        return _row_from_db(dict(found)) if found else None

    def _message_seen(self, tenant_id: str, name: str) -> bool:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pr_review_messages WHERE tenant_id = %s AND message_name = %s",
                (tenant_id, name),
            )
            return cur.fetchone() is not None

    def _record_message(
        self, tenant_id: str, name: str, kind: str, outcome: str, error: str
    ) -> None:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pr_review_messages (tenant_id, message_name, kind, outcome, error) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, message_name) DO NOTHING",
                (tenant_id, name, kind, outcome[:500], error[:1000]),
            )
            conn.commit()

    def _get_cursor(self, tenant_id: str, source: str) -> str:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT cursor FROM pr_review_cursors WHERE tenant_id = %s AND source = %s",
                (tenant_id, source),
            )
            found = cur.fetchone()
            return str(found[0]) if found else ""

    def _set_cursor(self, tenant_id: str, source: str, cursor: str) -> None:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pr_review_cursors (tenant_id, source, cursor) VALUES (%s, %s, %s) "
                "ON CONFLICT (tenant_id, source) DO UPDATE SET cursor = EXCLUDED.cursor, "
                "updated_at = now()",
                (tenant_id, source, cursor),
            )
            conn.commit()

    async def get(self, tenant_id: str, repo: str, number: int) -> PrReviewRow | None:
        rows = await asyncio.to_thread(
            self._select, tenant_id, "lower(repo) = lower(%s) AND number = %s", (repo, number)
        )
        return rows[0] if rows else None

    async def save(self, row: PrReviewRow) -> None:
        changed = _changed(row)
        if changed == []:
            return
        await asyncio.to_thread(self._save, copy.deepcopy(row), changed)
        _mark_loaded(row)

    async def transition(
        self,
        tenant_id: str,
        repo: str,
        number: int,
        *,
        from_statuses: Collection[str],
        to_status: str,
        job_id: str | None = None,
        set_job_id: str | None = None,
    ) -> PrReviewRow | None:
        return await asyncio.to_thread(
            self._transition,
            tenant_id,
            repo,
            number,
            sorted(from_statuses),
            to_status,
            job_id,
            set_job_id,
        )

    @contextlib.asynccontextmanager
    async def intake_lock(self, tenant_id: str) -> AsyncIterator[bool]:
        """A per-tenant session advisory lock held for the whole intake run, or False."""
        from robothor.db.connection import get_connection

        key = _INTAKE_LOCK_NS + tenant_id
        stack = contextlib.ExitStack()

        def _acquire() -> tuple[Any, bool]:
            conn = stack.enter_context(get_connection(autocommit=True))
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,))
                got = bool(cur.fetchone()[0])
            return conn, got

        def _release(conn: Any) -> None:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
            finally:
                stack.close()

        try:
            conn, got = await asyncio.to_thread(_acquire)
        except BaseException:
            stack.close()
            raise
        try:
            yield got
        finally:
            if got:
                await asyncio.shield(asyncio.to_thread(_release, conn))
            else:
                stack.close()

    async def list_by_thread(self, tenant_id: str, thread: str) -> list[PrReviewRow]:
        if not thread:
            return []
        return await asyncio.to_thread(self._select, tenant_id, "chat_thread = %s", (thread,))

    async def list_pending(self, tenant_id: str) -> list[PrReviewRow]:
        return await asyncio.to_thread(self._select, tenant_id, "pending_trigger <> ''", ())

    async def list_active(self, tenant_id: str) -> list[PrReviewRow]:
        return await asyncio.to_thread(
            self._select, tenant_id, "status IN ('queued', 'reviewing', 'posting')", ()
        )

    async def list_failed(self, tenant_id: str) -> list[PrReviewRow]:
        return await asyncio.to_thread(self._select, tenant_id, "status = 'failed'", ())

    async def message_seen(self, tenant_id: str, name: str) -> bool:
        return await asyncio.to_thread(self._message_seen, tenant_id, name)

    async def record_message(
        self, tenant_id: str, name: str, kind: str, outcome: str = "", error: str = ""
    ) -> None:
        await asyncio.to_thread(self._record_message, tenant_id, name, kind, outcome, error)

    async def get_cursor(self, tenant_id: str, source: str) -> str:
        return await asyncio.to_thread(self._get_cursor, tenant_id, source)

    async def set_cursor(self, tenant_id: str, source: str, cursor: str) -> None:
        await asyncio.to_thread(self._set_cursor, tenant_id, source, cursor)


def row_to_dict(row: PrReviewRow) -> dict[str, Any]:
    data = asdict(row)
    data["failed_at"] = row.failed_at.isoformat() if row.failed_at else None
    data["created_at"] = row.created_at.isoformat()
    data["updated_at"] = row.updated_at.isoformat()
    return data

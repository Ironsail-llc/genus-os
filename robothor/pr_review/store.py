"""Review state: the ``pr_reviews`` table and its two companions (migration 145).

* ``pr_reviews`` — one row per (tenant, repo, number): the head we know, the
  head we last reviewed, the head a task is queued for, status, Chat refs, our
  review ids and the last review's findings (for the next re-review).
* ``pr_review_messages`` — every Chat message the intake has handled, with
  what it did; a message is handled once, and a failing one is recorded and
  skipped rather than retried forever.
* ``pr_review_cursors`` — where each Chat source's last poll stopped.

:class:`MemoryStore` is the same contract in memory, for tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = [
    "ACTIVE_STATUSES",
    "MemoryStore",
    "PgStore",
    "PrReviewRow",
    "PrReviewStore",
]

#: A task exists and has not been finalized.
ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "reviewing"})


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
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


_COLUMNS: tuple[str, ...] = tuple(
    k for k in PrReviewRow.__dataclass_fields__ if k not in ("created_at", "updated_at")
)
_JSON_COLUMNS = frozenset({"review_ids", "last_review"})


class PrReviewStore(Protocol):
    async def get(self, tenant_id: str, repo: str, number: int) -> PrReviewRow | None: ...
    async def save(self, row: PrReviewRow) -> None: ...
    async def list_by_thread(self, tenant_id: str, thread: str) -> list[PrReviewRow]: ...
    async def list_pending(self, tenant_id: str) -> list[PrReviewRow]: ...
    async def list_active(self, tenant_id: str) -> list[PrReviewRow]: ...
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

    async def get(self, tenant_id: str, repo: str, number: int) -> PrReviewRow | None:
        row = self.rows.get((tenant_id, repo.lower(), number))
        return copy.deepcopy(row) if row else None

    async def save(self, row: PrReviewRow) -> None:
        row.updated_at = _now()
        self.rows[(row.tenant_id, row.repo.lower(), row.number)] = copy.deepcopy(row)

    def _all(self, tenant_id: str) -> list[PrReviewRow]:
        rows = [r for (t, _, _), r in self.rows.items() if t == tenant_id]
        return [copy.deepcopy(r) for r in sorted(rows, key=lambda r: r.updated_at)]

    async def list_by_thread(self, tenant_id: str, thread: str) -> list[PrReviewRow]:
        return [r for r in self._all(tenant_id) if thread and r.chat_thread == thread]

    async def list_pending(self, tenant_id: str) -> list[PrReviewRow]:
        return [r for r in self._all(tenant_id) if r.pending_trigger]

    async def list_active(self, tenant_id: str) -> list[PrReviewRow]:
        return [r for r in self._all(tenant_id) if r.status in ACTIVE_STATUSES]

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
    for key in ("url", "title", "author", "source", "head_sha", "last_reviewed_sha"):
        values[key] = values.get(key) or ""
    known = PrReviewRow.__dataclass_fields__
    return PrReviewRow(**{k: v for k, v in values.items() if k in known})


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

    def _save(self, row: PrReviewRow) -> None:
        values = []
        for col in _COLUMNS:
            value = getattr(row, col)
            values.append(json.dumps(value) if col in _JSON_COLUMNS else value)
        cols = ", ".join(_COLUMNS)
        marks = ", ".join(["%s"] * len(_COLUMNS))
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c not in ("tenant_id", "repo", "number")
        )
        with self._connect(row.tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO pr_reviews ({cols}) VALUES ({marks}) "
                f"ON CONFLICT (tenant_id, repo, number) DO UPDATE SET {updates}, "
                "updated_at = now()",
                values,
            )
            conn.commit()

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
        await asyncio.to_thread(self._save, copy.deepcopy(row))

    async def list_by_thread(self, tenant_id: str, thread: str) -> list[PrReviewRow]:
        return await asyncio.to_thread(self._select, tenant_id, "chat_thread = %s", (thread,))

    async def list_pending(self, tenant_id: str) -> list[PrReviewRow]:
        return await asyncio.to_thread(self._select, tenant_id, "pending_trigger <> ''", ())

    async def list_active(self, tenant_id: str) -> list[PrReviewRow]:
        return await asyncio.to_thread(
            self._select, tenant_id, "status IN ('queued', 'reviewing')", ()
        )

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
    data["created_at"] = row.created_at.isoformat()
    data["updated_at"] = row.updated_at.isoformat()
    return data

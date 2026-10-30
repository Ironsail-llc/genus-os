"""The n8n snapshot: what the poller last saw, for the dashboard and the agent tools.

Three tables (migration 156):

* ``n8n_workflows`` -- the instance's workflows as of the latest successful
  listing. A workflow missing from a listing has been deleted in n8n and
  leaves the snapshot.
* ``n8n_executions`` -- failed executions, one row each. ``alerted_at`` is
  stamped once the main agent has been told, which is what makes a
  notification once-only across rounds, restarts and leader changes.
* ``n8n_sync_state`` -- per tenant: when the poller last ran, when it last
  succeeded, the last error class and the workflow totals.

:class:`MemoryN8nStore` is the in-process store the unit tests use;
:class:`PgN8nStore` is the real one. Both keep the same rules.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

__all__ = [
    "RETENTION_DAYS",
    "MemoryN8nStore",
    "N8nStore",
    "N8nSyncState",
    "PgN8nStore",
]

#: How long a failed execution is kept (and how far back one is recorded).
RETENTION_DAYS = 30


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class N8nSyncState:
    """One tenant's poll position. ``last_ok_at`` None = never polled successfully."""

    tenant_id: str
    last_poll_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    workflows_total: int = 0
    workflows_active: int = 0


class N8nStore(Protocol):
    async def upsert_workflows(self, tenant_id: str, rows: Iterable[Mapping[str, Any]]) -> None: ...
    async def record_executions(
        self, tenant_id: str, rows: Iterable[Mapping[str, Any]]
    ) -> list[str]: ...
    async def set_error_messages(self, tenant_id: str, messages: Mapping[str, str]) -> None: ...
    async def pending_alerts(self, tenant_id: str) -> list[dict[str, Any]]: ...
    async def mark_alerted(self, tenant_id: str, execution_ids: Iterable[str]) -> None: ...
    async def set_sync_state(
        self,
        tenant_id: str,
        *,
        polled_at: datetime,
        ok: bool,
        error: str | None = None,
        workflows_total: int | None = None,
        workflows_active: int | None = None,
    ) -> None: ...
    async def get_sync_state(self, tenant_id: str) -> N8nSyncState | None: ...
    async def list_workflows(
        self, tenant_id: str, *, now: datetime | None = None
    ) -> list[dict[str, Any]]: ...
    async def list_executions(
        self,
        tenant_id: str,
        workflow_id: str | None = None,
        limit: int = 20,
        *,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]: ...
    async def purge(self, tenant_id: str, older_than_days: int = RETENTION_DAYS) -> int: ...


_EXECUTION_FIELDS = (
    "execution_id",
    "workflow_id",
    "status",
    "mode",
    "started_at",
    "stopped_at",
    "error_message",
)


def _at(row: Mapping[str, Any]) -> datetime:
    """When an execution happened: its start, or when the poller first saw it."""
    when = row.get("started_at") or row.get("seen_at")
    assert isinstance(when, datetime)
    return when


class MemoryN8nStore:
    """The store in dicts. Same semantics as the database, for tests."""

    def __init__(self, *, now: Callable[[], datetime] = _now) -> None:
        self.workflows: dict[tuple[str, str], dict[str, Any]] = {}
        self.executions: dict[tuple[str, str], dict[str, Any]] = {}
        self.states: dict[str, N8nSyncState] = {}
        self._now = now

    async def upsert_workflows(self, tenant_id: str, rows: Iterable[Mapping[str, Any]]) -> None:
        listed = {str(r["workflow_id"]): dict(r) for r in rows}
        for key in [k for k in self.workflows if k[0] == tenant_id and k[1] not in listed]:
            del self.workflows[key]
        for wid, row in listed.items():
            row["last_seen_at"] = self._now()
            self.workflows[(tenant_id, wid)] = row

    async def record_executions(
        self, tenant_id: str, rows: Iterable[Mapping[str, Any]]
    ) -> list[str]:
        new: list[str] = []
        for row in rows:
            key = (tenant_id, str(row["execution_id"]))
            if key in self.executions:
                continue
            stored = {field: row.get(field) for field in _EXECUTION_FIELDS}
            stored.update(seen_at=self._now(), alerted_at=None)
            self.executions[key] = stored
            new.append(key[1])
        return new

    async def set_error_messages(self, tenant_id: str, messages: Mapping[str, str]) -> None:
        for eid, message in messages.items():
            row = self.executions.get((tenant_id, eid))
            if row is not None and message:
                row["error_message"] = message

    async def pending_alerts(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for (t, _), r in self.executions.items()
            if t == tenant_id and r["alerted_at"] is None
        ]
        return sorted(rows, key=_at)

    async def mark_alerted(self, tenant_id: str, execution_ids: Iterable[str]) -> None:
        for eid in execution_ids:
            row = self.executions.get((tenant_id, eid))
            if row is not None and row["alerted_at"] is None:
                row["alerted_at"] = self._now()

    async def set_sync_state(
        self,
        tenant_id: str,
        *,
        polled_at: datetime,
        ok: bool,
        error: str | None = None,
        workflows_total: int | None = None,
        workflows_active: int | None = None,
    ) -> None:
        state = self.states.get(tenant_id) or N8nSyncState(tenant_id)
        changes: dict[str, Any] = {"last_poll_at": polled_at, "last_error": None if ok else error}
        if ok:
            changes["last_ok_at"] = polled_at
        if workflows_total is not None:
            changes["workflows_total"] = workflows_total
        if workflows_active is not None:
            changes["workflows_active"] = workflows_active
        self.states[tenant_id] = replace(state, **changes)

    async def get_sync_state(self, tenant_id: str) -> N8nSyncState | None:
        return self.states.get(tenant_id)

    async def list_workflows(
        self, tenant_id: str, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        current = now or self._now()
        day, week = current - timedelta(hours=24), current - timedelta(days=7)
        out: list[dict[str, Any]] = []
        for (t, wid), wf in self.workflows.items():
            if t != tenant_id:
                continue
            runs = sorted(
                (
                    r
                    for (et, _), r in self.executions.items()
                    if et == t and r["workflow_id"] == wid
                ),
                key=lambda r: (_at(r), str(r["execution_id"])),
                reverse=True,
            )
            out.append(
                {
                    "workflow_id": wid,
                    "name": wf.get("name") or "",
                    "active": bool(wf.get("active")),
                    "tags": list(wf.get("tags") or []),
                    "remote_updated_at": wf.get("remote_updated_at"),
                    "failures_24h": sum(1 for r in runs if _at(r) >= day),
                    "failures_7d": sum(1 for r in runs if _at(r) >= week),
                    "last_failure_at": _at(runs[0]) if runs else None,
                    "last_error": runs[0].get("error_message") if runs else None,
                    "last_failed_execution_id": runs[0]["execution_id"] if runs else None,
                }
            )
        return sorted(out, key=lambda w: (str(w["name"]).lower(), w["workflow_id"]))

    async def list_executions(
        self,
        tenant_id: str,
        workflow_id: str | None = None,
        limit: int = 20,
        *,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        matching = sorted(
            (
                r
                for (t, _), r in self.executions.items()
                if t == tenant_id
                and (not workflow_id or r["workflow_id"] == workflow_id)
                and (since is None or _at(r) >= since)
            ),
            key=_at,
            reverse=True,
        )
        return [{f: r.get(f) for f in _EXECUTION_FIELDS} for r in matching[: max(0, limit)]]

    async def purge(self, tenant_id: str, older_than_days: int = RETENTION_DAYS) -> int:
        cutoff = self._now() - timedelta(days=older_than_days)
        stale = [k for k, r in self.executions.items() if k[0] == tenant_id and _at(r) < cutoff]
        for key in stale:
            del self.executions[key]
        return len(stale)


# ── PostgreSQL ────────────────────────────────────────────────────────────────

_WHEN = "COALESCE(started_at, seen_at)"


class PgN8nStore:
    """The three tables, tenant-scoped through the RLS binding and an explicit filter.

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

    # -- blocking halves ---------------------------------------------------

    def _upsert_workflows(self, tenant_id: str, rows: list[dict[str, Any]]) -> None:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    "INSERT INTO n8n_workflows (tenant_id, workflow_id, name, active, tags, "
                    "remote_updated_at, last_seen_at) VALUES (%s, %s, %s, %s, %s::jsonb, %s, now()) "
                    "ON CONFLICT (tenant_id, workflow_id) DO UPDATE SET name = EXCLUDED.name, "
                    "active = EXCLUDED.active, tags = EXCLUDED.tags, "
                    "remote_updated_at = EXCLUDED.remote_updated_at, last_seen_at = now()",
                    (
                        tenant_id,
                        str(row["workflow_id"]),
                        row.get("name") or "",
                        bool(row.get("active")),
                        json.dumps(list(row.get("tags") or [])),
                        row.get("remote_updated_at"),
                    ),
                )
            cur.execute(
                "DELETE FROM n8n_workflows WHERE tenant_id = %s AND NOT (workflow_id = ANY(%s))",
                (tenant_id, [str(r["workflow_id"]) for r in rows]),
            )
            conn.commit()

    def _record_executions(self, tenant_id: str, rows: list[dict[str, Any]]) -> list[str]:
        new: list[str] = []
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    "INSERT INTO n8n_executions (tenant_id, execution_id, workflow_id, status, "
                    "mode, started_at, stopped_at, error_message) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, execution_id) DO NOTHING RETURNING execution_id",
                    (
                        tenant_id,
                        str(row["execution_id"]),
                        str(row.get("workflow_id") or ""),
                        row.get("status") or "error",
                        row.get("mode") or "",
                        row.get("started_at"),
                        row.get("stopped_at"),
                        row.get("error_message"),
                    ),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    new.append(str(inserted[0]))
            conn.commit()
        return new

    def _set_error_messages(self, tenant_id: str, messages: dict[str, str]) -> None:
        if not messages:
            return
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            for eid, message in messages.items():
                if message:
                    cur.execute(
                        "UPDATE n8n_executions SET error_message = %s "
                        "WHERE tenant_id = %s AND execution_id = %s",
                        (message, tenant_id, eid),
                    )
            conn.commit()

    def _executions(self, tenant_id: str, where: str, params: tuple[Any, ...], tail: str) -> Any:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT execution_id, workflow_id, status, mode, started_at, stopped_at, "
                f"error_message FROM n8n_executions WHERE tenant_id = %s {where} {tail}",
                (tenant_id, *params),
            )
            return [dict(zip(_EXECUTION_FIELDS, row, strict=True)) for row in cur.fetchall()]

    def _mark_alerted(self, tenant_id: str, ids: list[str]) -> None:
        if not ids:
            return
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE n8n_executions SET alerted_at = now() WHERE tenant_id = %s "
                "AND execution_id = ANY(%s) AND alerted_at IS NULL",
                (tenant_id, ids),
            )
            conn.commit()

    def _set_sync_state(
        self,
        tenant_id: str,
        polled_at: datetime,
        ok: bool,
        error: str | None,
        total: int | None,
        active: int | None,
    ) -> None:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO n8n_sync_state (tenant_id, last_poll_at, last_ok_at, last_error, "
                "workflows_total, workflows_active) VALUES (%s, %s, %s, %s, "
                "COALESCE(%s, 0), COALESCE(%s, 0)) "
                "ON CONFLICT (tenant_id) DO UPDATE SET last_poll_at = EXCLUDED.last_poll_at, "
                "last_ok_at = COALESCE(EXCLUDED.last_ok_at, n8n_sync_state.last_ok_at), "
                "last_error = EXCLUDED.last_error, "
                "workflows_total = COALESCE(%s, n8n_sync_state.workflows_total), "
                "workflows_active = COALESCE(%s, n8n_sync_state.workflows_active)",
                (
                    tenant_id,
                    polled_at,
                    polled_at if ok else None,
                    None if ok else error,
                    total,
                    active,
                    total,
                    active,
                ),
            )
            conn.commit()

    def _get_sync_state(self, tenant_id: str) -> N8nSyncState | None:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_poll_at, last_ok_at, last_error, workflows_total, workflows_active "
                "FROM n8n_sync_state WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return N8nSyncState(
            tenant_id=tenant_id,
            last_poll_at=row[0],
            last_ok_at=row[1],
            last_error=row[2],
            workflows_total=int(row[3] or 0),
            workflows_active=int(row[4] or 0),
        )

    def _list_workflows(self, tenant_id: str, now: datetime) -> list[dict[str, Any]]:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT w.workflow_id, w.name, w.active, w.tags, w.remote_updated_at, "
                "COUNT(e.execution_id) FILTER (WHERE COALESCE(e.started_at, e.seen_at) >= %s), "
                "COUNT(e.execution_id) FILTER (WHERE COALESCE(e.started_at, e.seen_at) >= %s), "
                "MAX(COALESCE(e.started_at, e.seen_at)), l.error_message, l.execution_id "
                "FROM n8n_workflows w "
                "LEFT JOIN LATERAL (SELECT error_message, execution_id FROM n8n_executions "
                "WHERE tenant_id = w.tenant_id AND workflow_id = w.workflow_id "
                f"ORDER BY {_WHEN} DESC, execution_id DESC LIMIT 1) l ON true "
                "LEFT JOIN n8n_executions e "
                "ON e.tenant_id = w.tenant_id AND e.workflow_id = w.workflow_id "
                "WHERE w.tenant_id = %s "
                "GROUP BY w.tenant_id, w.workflow_id, l.error_message, l.execution_id "
                "ORDER BY lower(w.name), w.workflow_id",
                (now - timedelta(hours=24), now - timedelta(days=7), tenant_id),
            )
            rows = cur.fetchall()
        return [
            {
                "workflow_id": r[0],
                "name": r[1],
                "active": bool(r[2]),
                "tags": list(r[3] or []),
                "remote_updated_at": r[4],
                "failures_24h": int(r[5] or 0),
                "failures_7d": int(r[6] or 0),
                "last_failure_at": r[7],
                "last_error": r[8],
                "last_failed_execution_id": r[9],
            }
            for r in rows
        ]

    def _purge(self, tenant_id: str, older_than_days: int) -> int:
        with self._connect(tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM n8n_executions WHERE tenant_id = %s AND {_WHEN} < %s",
                (tenant_id, _now() - timedelta(days=older_than_days)),
            )
            count = int(cur.rowcount or 0)
            conn.commit()
        return count

    # -- async surface -----------------------------------------------------

    async def upsert_workflows(self, tenant_id: str, rows: Iterable[Mapping[str, Any]]) -> None:
        await asyncio.to_thread(self._upsert_workflows, tenant_id, [dict(r) for r in rows])

    async def record_executions(
        self, tenant_id: str, rows: Iterable[Mapping[str, Any]]
    ) -> list[str]:
        return await asyncio.to_thread(self._record_executions, tenant_id, [dict(r) for r in rows])

    async def set_error_messages(self, tenant_id: str, messages: Mapping[str, str]) -> None:
        await asyncio.to_thread(self._set_error_messages, tenant_id, dict(messages))

    async def pending_alerts(self, tenant_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await asyncio.to_thread(
            self._executions, tenant_id, "AND alerted_at IS NULL", (), f"ORDER BY {_WHEN}"
        )
        return rows

    async def mark_alerted(self, tenant_id: str, execution_ids: Iterable[str]) -> None:
        await asyncio.to_thread(self._mark_alerted, tenant_id, [str(i) for i in execution_ids])

    async def set_sync_state(
        self,
        tenant_id: str,
        *,
        polled_at: datetime,
        ok: bool,
        error: str | None = None,
        workflows_total: int | None = None,
        workflows_active: int | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._set_sync_state,
            tenant_id,
            polled_at,
            ok,
            error,
            workflows_total,
            workflows_active,
        )

    async def get_sync_state(self, tenant_id: str) -> N8nSyncState | None:
        return await asyncio.to_thread(self._get_sync_state, tenant_id)

    async def list_workflows(
        self, tenant_id: str, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_workflows, tenant_id, now or _now())

    async def list_executions(
        self,
        tenant_id: str,
        workflow_id: str | None = None,
        limit: int = 20,
        *,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        where, params = "", list[Any]()
        if workflow_id:
            where += " AND workflow_id = %s"
            params.append(workflow_id)
        if since is not None:
            where += f" AND {_WHEN} >= %s"
            params.append(since)
        params.append(max(0, int(limit)))
        rows: list[dict[str, Any]] = await asyncio.to_thread(
            self._executions, tenant_id, where, tuple(params), f"ORDER BY {_WHEN} DESC LIMIT %s"
        )
        return rows

    async def purge(self, tenant_id: str, older_than_days: int = RETENTION_DAYS) -> int:
        return await asyncio.to_thread(self._purge, tenant_id, older_than_days)

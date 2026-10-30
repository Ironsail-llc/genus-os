"""
Genus OS Audit Log — Structured audit logging for all system operations.

Event types:
  - crm.create, crm.update, crm.delete, crm.merge — CRM mutations
  - agent.action — Agent tool invocations
  - service.health — Health check results and telemetry
  - ipc.message — Inter-process communication events
  - auth.access, auth.denied — Access control events
  - system.boot, system.error — System lifecycle events

Usage:
    from robothor.audit.logger import log_event, query_log, stats
    log_event("crm.create", "Created person John Doe",
              actor="crm-steward", target="person:uuid", details={...})
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg2
from psycopg2.extras import Json

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger = logging.getLogger(__name__)

# Lazy connection resolution — don't import config at module level so tests can mock
_conn_factory = None


@dataclass(frozen=True)
class RequestActor:
    """Who a request acts as, for audit rows written beneath it."""

    user_id: str
    home_tenant_id: str
    tenant_id: str


#: The verified caller of the request this context is serving, if any. Set by
#: the bridge's ``TenantScopeMiddleware``; read by :func:`log_event` when its
#: caller names no actor — the DAL's CRM audit (``_safe_audit``) never does, so
#: before this a root owner's write in a child tenant was filed as ``robothor``.
_request_actor: ContextVar[RequestActor | None] = ContextVar("audit_request_actor", default=None)


@contextlib.contextmanager
def request_actor(*, user_id: str, home_tenant_id: str, tenant_id: str) -> Iterator[None]:
    """Attribute every audit row written in this context to this caller."""
    token = _request_actor.set(
        RequestActor(user_id=user_id, home_tenant_id=home_tenant_id, tenant_id=tenant_id)
    )
    try:
        yield
    finally:
        _request_actor.reset(token)


def _get_connection() -> Any:
    """Get a database connection using robothor.db or fallback DSN."""
    if _conn_factory is not None:
        return _conn_factory()

    try:
        from robothor.db.connection import get_pool

        pool = get_pool()
        return pool.getconn()
    except Exception as e:
        logger.warning("Audit: pool unavailable, using direct DSN: %s", e)
        from robothor.config import get_config
        from robothor.db.connection import assert_test_database

        cfg = get_config()
        # The pool guard above can be routed around by this fallback — apply the
        # same "never touch a non-test database from pytest" check here.
        assert_test_database(cfg.db.name)
        return psycopg2.connect(cfg.db.dsn)


def _release_connection(conn: Any) -> None:
    """Return connection to pool if using pooled connections."""
    try:
        from robothor.db.connection import get_pool

        pool = get_pool()
        pool.putconn(conn)
    except Exception:
        conn.close()


def _writing_tenant(explicit: str | None) -> str:
    """The tenant an audit row is recorded under.

    The caller's, if it names one; else the enclosing ``tenant_scope`` — which
    for a bridge request is the ACTING tenant; else this process's own tenant.
    """
    if explicit:
        return explicit
    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import current_tenant_scope, effective_tenant

    return current_tenant_scope() or effective_tenant() or DEFAULT_TENANT


def _tenant_clause(
    tenants: Sequence[str] | None, include_untenanted: bool
) -> tuple[str, list[Any]]:
    """``(" AND ...", params)`` restricting a read to these tenants, or nothing.

    ``tenants=None`` is an unfiltered read (internal callers). An EMPTY list
    reads nothing rather than everything. Rows written before ``audit_log``
    had a tenant are NULL — appliance-wide — and are included only on request.
    """
    if tenants is None:
        return "", []
    if include_untenanted:
        return " AND (tenant_id = ANY(%s) OR tenant_id IS NULL)", [list(tenants)]
    return " AND tenant_id = ANY(%s)", [list(tenants)]


def set_connection_factory(factory: Any) -> None:
    """Override connection factory for testing."""
    global _conn_factory
    _conn_factory = factory


def reset_connection_factory() -> None:
    """Reset connection factory to default."""
    global _conn_factory
    _conn_factory = None


def log_event(
    event_type: str,
    action: str,
    *,
    category: str | None = None,
    actor: str = "robothor",
    user_id: str = "",
    session_key: str | None = None,
    details: dict[str, Any] | None = None,
    source_channel: str | None = None,
    target: str | None = None,
    status: str = "ok",
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Log a structured audit event.

    ``tenant_id`` defaults to the enclosing ``tenant_scope`` (a bridge
    request's acting tenant), then the process's own tenant — see
    :func:`_writing_tenant`. Inside a request (:func:`request_actor`), a
    caller that names no ``actor`` / ``user_id`` gets the request's verified
    caller instead of ``robothor`` / ``""``, and an acting caller's home tenant
    is added to ``details``.

    Returns {"id": int, "timestamp": str} on success, None on failure.
    Failures are logged but never raise — audit must not break callers.
    """
    requester = _request_actor.get()
    if requester is not None:
        if actor == "robothor":
            actor = requester.user_id
        if not user_id:
            user_id = requester.user_id
        if requester.home_tenant_id != requester.tenant_id:
            details = {"home_tenant_id": requester.home_tenant_id, **(details or {})}
    conn = None
    tenant = _writing_tenant(tenant_id)
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO audit_log
                (event_type, category, actor, action, details,
                 source_channel, target, status, session_key, user_id, tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, timestamp
            """,
            (
                event_type,
                category,
                actor,
                action,
                Json(details) if details else None,
                source_channel,
                target,
                status,
                session_key,
                user_id,
                tenant,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        result = {"id": row[0], "timestamp": row[1].isoformat()}
        # Best-effort fan-out to an external SIEM (env-gated; never blocks).
        with contextlib.suppress(Exception):
            from robothor.audit.siem import forward_event, siem_enabled

            if siem_enabled():
                forward_event(
                    {
                        "id": row[0],
                        "timestamp": result["timestamp"],
                        "event_type": event_type,
                        "category": category,
                        "actor": actor,
                        "action": action,
                        "details": details,
                        "source_channel": source_channel,
                        "target": target,
                        "status": status,
                        "session_key": session_key,
                        "user_id": user_id,
                        "tenant_id": tenant,
                    }
                )
        return result
    except Exception as e:
        logger.warning("Audit log_event failed: %s", e)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        return None
    finally:
        if conn is not None:
            _release_connection(conn)


def log_crm_mutation(
    operation: str,
    entity_type: str,
    entity_id: str | None,
    *,
    actor: str = "robothor",
    details: dict[str, Any] | None = None,
    status: str = "ok",
) -> dict[str, Any] | None:
    """Convenience wrapper for CRM mutations.

    operation: create, update, delete, merge
    entity_type: person, company, note, task, conversation, message
    """
    event_type = f"crm.{operation}"
    target = f"{entity_type}:{entity_id}" if entity_id else entity_type
    action_str = f"{operation} {entity_type}"
    if entity_id:
        action_str += f" {entity_id}"
    return log_event(
        event_type,
        action_str,
        actor=actor,
        target=target,
        details=details,
        status=status,
        category="crm",
    )


def query_log(
    limit: int = 50,
    event_type: str | None = None,
    category: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    since: str | None = None,
    status: str | None = None,
    user_id: str | None = None,
    *,
    tenants: Sequence[str] | None = None,
    include_untenanted: bool = False,
) -> list[dict[str, Any]]:
    """Query audit log with filters.

    ``tenants`` restricts the read to rows recorded under those tenants (and,
    with ``include_untenanted``, to the NULL rows that predate the column).
    """
    conn = None
    try:
        conn = _get_connection()
        cur = conn.cursor()

        query = (
            "SELECT id, timestamp, event_type, category, actor, action, "
            "details, source_channel, target, status, session_key, user_id "
            "FROM audit_log WHERE 1=1"
        )
        params: list[Any] = []

        if event_type:
            query += " AND event_type = %s"
            params.append(event_type)
        if category:
            query += " AND category = %s"
            params.append(category)
        if actor:
            query += " AND actor = %s"
            params.append(actor)
        if target:
            query += " AND target LIKE %s"
            params.append(f"%{target}%")
        if since:
            query += " AND timestamp >= %s"
            params.append(since)
        if status:
            query += " AND status = %s"
            params.append(status)
        if user_id:
            query += " AND user_id = %s"
            params.append(user_id)
        clause, clause_params = _tenant_clause(tenants, include_untenanted)
        query += clause
        params.extend(clause_params)

        query += " ORDER BY timestamp DESC LIMIT %s"
        params.append(limit)

        cur.execute(query, params)
        rows = cur.fetchall()

        return [
            {
                "id": r[0],
                "timestamp": r[1].isoformat(),
                "event_type": r[2],
                "category": r[3],
                "actor": r[4],
                "action": r[5],
                "details": r[6],
                "source_channel": r[7],
                "target": r[8],
                "status": r[9],
                "session_key": r[10],
                "user_id": r[11] if len(r) > 11 else "",
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Audit query_log failed: %s", e)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        return []
    finally:
        if conn is not None:
            _release_connection(conn)


def stats(
    *, tenants: Sequence[str] | None = None, include_untenanted: bool = False
) -> dict[str, Any]:
    """Get audit log statistics, over the same tenant filter as :func:`query_log`."""
    clause, params = _tenant_clause(tenants, include_untenanted)
    conn = None
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS total, COUNT(DISTINCT event_type) AS event_types, "
            "MIN(timestamp) AS earliest, MAX(timestamp) AS latest "
            "FROM audit_log WHERE 1=1" + clause,
            params,
        )
        row = cur.fetchone()

        cur.execute(
            "SELECT event_type, COUNT(*) FROM audit_log WHERE 1=1" + clause + " "
            "GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 20",
            params,
        )
        by_type = cur.fetchall()

        return {
            "total_events": row[0],
            "unique_event_types": row[1],
            "earliest": row[2].isoformat() if row[2] else None,
            "latest": row[3].isoformat() if row[3] else None,
            "by_type": {r[0]: r[1] for r in by_type},
        }
    except Exception as e:
        logger.warning("Audit stats failed: %s", e)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        return {"total_events": 0, "error": str(e)}
    finally:
        if conn is not None:
            _release_connection(conn)


def log_telemetry(
    service: str,
    metric: str,
    value: float,
    *,
    unit: str | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """Write a telemetry data point to the telemetry table.

    Returns True on success.
    """
    conn = None
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO telemetry (service, metric, value, unit, details)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (service, metric, value, unit, Json(details) if details else None),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("Telemetry write failed: %s", e)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        return False
    finally:
        if conn is not None:
            _release_connection(conn)


def query_telemetry(
    service: str | None = None,
    metric: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query telemetry data points."""
    conn = None
    try:
        conn = _get_connection()
        cur = conn.cursor()

        query = (
            "SELECT id, timestamp, service, metric, value, unit, details FROM telemetry WHERE 1=1"
        )
        params: list[Any] = []

        if service:
            query += " AND service = %s"
            params.append(service)
        if metric:
            query += " AND metric = %s"
            params.append(metric)
        if since:
            query += " AND timestamp >= %s"
            params.append(since)

        query += " ORDER BY timestamp DESC LIMIT %s"
        params.append(limit)

        cur.execute(query, params)
        rows = cur.fetchall()

        return [
            {
                "id": r[0],
                "timestamp": r[1].isoformat(),
                "service": r[2],
                "metric": r[3],
                "value": float(r[4]),
                "unit": r[5],
                "details": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Telemetry query failed: %s", e)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        return []
    finally:
        if conn is not None:
            _release_connection(conn)

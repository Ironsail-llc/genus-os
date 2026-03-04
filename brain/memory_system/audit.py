#!/usr/bin/env python3
"""
Robothor Audit Log — Structured audit logging for all system operations.

Event types:
  - crm.create, crm.update, crm.delete, crm.merge — CRM mutations
  - agent.action — Agent tool invocations
  - service.health — Health check results and telemetry
  - ipc.message — Inter-process communication events
  - auth.access, auth.denied — Access control events
  - system.boot, system.error — System lifecycle events

Usage:
    from audit import log_event, query_log, stats
    log_event("crm.create", "Created person John Doe",
              actor="crm-steward", target="person:uuid", details={...})
"""

import json
import logging
import sys

import psycopg2
from psycopg2.extras import Json

logger = logging.getLogger(__name__)

# Lazy DSN resolution — don't import config at module level so tests can mock
_pg_dsn = None


def _get_dsn():
    global _pg_dsn
    if _pg_dsn is None:
        try:
            import config

            _pg_dsn = config.PG_DSN
        except (ImportError, AttributeError):
            _pg_dsn = "dbname=robothor_memory user=philip host=/var/run/postgresql"
    return _pg_dsn


def set_dsn(dsn: str):
    """Override DSN for testing."""
    global _pg_dsn
    _pg_dsn = dsn


def _conn():
    return psycopg2.connect(_get_dsn())


def log_event(
    event_type: str,
    action: str,
    *,
    category: str | None = None,
    actor: str = "robothor",
    session_key: str | None = None,
    details: dict | None = None,
    source_channel: str | None = None,
    target: str | None = None,
    status: str = "ok",
) -> dict | None:
    """Log a structured audit event.

    Returns {"id": int, "timestamp": str} on success, None on failure.
    Failures are logged but never raise — audit must not break callers.
    """
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO audit_log
                (event_type, category, actor, action, details,
                 source_channel, target, status, session_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            ),
        )
        row = cur.fetchone()
        conn.commit()
        conn.close()
        return {"id": row[0], "timestamp": row[1].isoformat()}
    except Exception as e:
        logger.warning("Audit log_event failed: %s", e)
        return None


def log_crm_mutation(
    operation: str,
    entity_type: str,
    entity_id: str | None,
    *,
    actor: str = "robothor",
    details: dict | None = None,
    status: str = "ok",
) -> dict | None:
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
) -> list[dict]:
    """Query audit log with filters."""
    try:
        conn = _conn()
        cur = conn.cursor()

        query = (
            "SELECT id, timestamp, event_type, category, actor, action, "
            "details, source_channel, target, status, session_key "
            "FROM audit_log WHERE 1=1"
        )
        params: list = []

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

        query += " ORDER BY timestamp DESC LIMIT %s"
        params.append(limit)

        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

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
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Audit query_log failed: %s", e)
        return []


def stats() -> dict:
    """Get audit log statistics."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(DISTINCT event_type) as event_types,
                MIN(timestamp) as earliest,
                MAX(timestamp) as latest
            FROM audit_log
        """)
        row = cur.fetchone()

        cur.execute("""
            SELECT event_type, COUNT(*) FROM audit_log
            GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 20
        """)
        by_type = cur.fetchall()
        conn.close()

        return {
            "total_events": row[0],
            "unique_event_types": row[1],
            "earliest": row[2].isoformat() if row[2] else None,
            "latest": row[3].isoformat() if row[3] else None,
            "by_type": {r[0]: r[1] for r in by_type},
        }
    except Exception as e:
        logger.warning("Audit stats failed: %s", e)
        return {"total_events": 0, "error": str(e)}


def log_telemetry(
    service: str,
    metric: str,
    value: float,
    *,
    unit: str | None = None,
    details: dict | None = None,
) -> bool:
    """Write a telemetry data point to the telemetry table.

    Returns True on success.
    """
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO telemetry (service, metric, value, unit, details)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (service, metric, value, unit, Json(details) if details else None),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning("Telemetry write failed: %s", e)
        return False


def query_telemetry(
    service: str | None = None,
    metric: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query telemetry data points."""
    try:
        conn = _conn()
        cur = conn.cursor()

        query = (
            "SELECT id, timestamp, service, metric, value, unit, details FROM telemetry WHERE 1=1"
        )
        params: list = []

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
        conn.close()

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
        return []


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: audit.py <command> [args]")
        print("Commands: log, query, stats")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "log" and len(sys.argv) >= 4:
        event_type = sys.argv[2]
        action = sys.argv[3]
        category = sys.argv[4] if len(sys.argv) > 4 else None
        result = log_event(event_type, action, category=category)
        print(json.dumps(result))

    elif cmd == "query":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        results = query_log(limit=limit)
        for r in results:
            print(f"[{r['timestamp'][:19]}] {r['event_type']:20} | {r['action'][:60]}")

    elif cmd == "stats":
        print(json.dumps(stats(), indent=2))

    else:
        print("Unknown command or missing arguments")
        sys.exit(1)

"""Data retention — tiered cleanup policy for all operational tables.

Runs daily from the daemon watchdog. Deletes expired rows in batches
to avoid holding table locks. Child tables are cleaned before parents
so FK cascades work correctly.

Usage:
    from robothor.engine.retention import run_retention_cleanup
    results = run_retention_cleanup()  # {table: rows_deleted}
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

# Retention policy — ordered children-first for FK cascade safety.
# Each entry: table_name → {days, timestamp_col, batch_size, extra_where?}
RETENTION_POLICY: OrderedDict[str, dict[str, Any]] = OrderedDict(
    [
        # ── Hot tier (30 days) — high-volume detail tables ──
        (
            "agent_run_steps",
            {"days": 30, "timestamp_col": "created_at", "batch_size": 5000},
        ),
        (
            "agent_run_checkpoints",
            {"days": 30, "timestamp_col": "created_at", "batch_size": 5000},
        ),
        (
            "agent_guardrail_events",
            {"days": 30, "timestamp_col": "created_at", "batch_size": 5000},
        ),
        # ── Warm tier (90 days) — operational audit trail ──
        (
            "audit_log",
            {"days": 90, "timestamp_col": "timestamp", "batch_size": 10000},
        ),
        (
            "telemetry",
            {"days": 90, "timestamp_col": "timestamp", "batch_size": 10000},
        ),
        (
            "workflow_run_steps",
            {"days": 90, "timestamp_col": "created_at", "batch_size": 5000},
        ),
        (
            "ingested_items",
            {"days": 90, "timestamp_col": "ingested_at", "batch_size": 5000},
        ),
        (
            "federation_events",
            {
                "days": 90,
                "timestamp_col": "created_at",
                "batch_size": 5000,
                "extra_where": "synced_at IS NOT NULL",
            },
        ),
        (
            "autodream_runs",
            {"days": 90, "timestamp_col": "started_at", "batch_size": 1000},
        ),
        # ── Cool tier (180 days) — summary-level records ──
        # Parent tables last — CASCADE will take remaining children
        (
            "agent_runs",
            {
                "days": 180,
                "timestamp_col": "created_at",
                "batch_size": 1000,
                "extra_where": "status IN ('completed', 'failed', 'timeout', 'cancelled', 'skipped')",
            },
        ),
        (
            "workflow_runs",
            {
                "days": 180,
                "timestamp_col": "created_at",
                "batch_size": 1000,
                "extra_where": "status IN ('completed', 'failed', 'timeout', 'cancelled')",
            },
        ),
    ]
)

# Allowlist of tables the cleanup is permitted to touch.
# Safety measure against SQL injection via misconfigured policy.
_ALLOWED_TABLES = frozenset(RETENTION_POLICY.keys())


def _cleanup_table(
    table: str,
    days: int,
    timestamp_col: str,
    batch_size: int = 5000,
    extra_where: str | None = None,
) -> int:
    """Delete rows older than *days* in batches. Returns total rows deleted.

    Uses a ctid-based subquery to grab a limited batch of row pointers,
    deletes exactly those, and commits. Each batch holds a lock only briefly.
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Table {table!r} is not in the retention allowlist")

    from robothor.db.connection import get_connection

    where = f"{timestamp_col} < NOW() - make_interval(days => {int(days)})"
    if extra_where:
        where = f"{where} AND {extra_where}"

    total = 0
    with get_connection() as conn:
        while True:
            cur = conn.cursor()
            cur.execute(
                f"DELETE FROM {table} WHERE ctid = ANY("  # noqa: S608
                f"  ARRAY(SELECT ctid FROM {table} WHERE {where} LIMIT %s)"
                f")",
                (batch_size,),
            )
            batch_deleted = cur.rowcount
            conn.commit()
            total += batch_deleted
            if batch_deleted < batch_size:
                break
    return total


def run_retention_cleanup() -> dict[str, int]:
    """Execute the full retention policy across all tables.

    Returns a dict mapping table_name → rows_deleted.
    Per-table failures are caught and logged (cleanup never raises).
    """
    results: dict[str, int] = {}
    for table, policy in RETENTION_POLICY.items():
        try:
            deleted = _cleanup_table(
                table,
                days=policy["days"],
                timestamp_col=policy["timestamp_col"],
                batch_size=policy.get("batch_size", 5000),
                extra_where=policy.get("extra_where"),
            )
            results[table] = deleted
            if deleted > 0:
                logger.info(
                    "Retention: deleted %d rows from %s (>%d days)",
                    deleted,
                    table,
                    policy["days"],
                )
        except Exception as e:
            logger.warning("Retention cleanup failed for %s: %s", table, e)
            results[table] = -1
    return results

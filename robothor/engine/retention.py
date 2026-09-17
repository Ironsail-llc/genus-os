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
import re
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

# Retention policy — ordered children-first for FK cascade safety.
# Each entry: table_name → {days, timestamp_col, batch_size, extra_where?,
# action?, set_clause?}. action defaults to "delete"; "update" applies
# set_clause to expired rows instead of deleting them (set_clause must make
# the rows stop matching the WHERE so the batch loop terminates).
RETENTION_POLICY: OrderedDict[str, dict[str, Any]] = OrderedDict(
    [
        # ── Hot tier (30 days) — high-volume detail tables ──
        (
            "agent_run_steps",
            {"days": 30, "timestamp_col": "created_at", "batch_size": 5000},
        ),
        (
            "delphi_market_snapshots",
            # Delphi shadow-pipeline collector: ~60-95k rows/day of orderbook
            # JSONB with no unbounded-history consumer. Instance-land table —
            # cleanup no-ops harmlessly where it doesn't exist.
            {"days": 30, "timestamp_col": "ts", "batch_size": 5000},
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
        (
            "delphi_intents",
            {"days": 90, "timestamp_col": "created_at", "batch_size": 5000},
        ),
        (
            "delphi_estimates",
            {"days": 90, "timestamp_col": "ts", "batch_size": 5000},
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
        # ── UPDATE-based policies — hygiene without data loss ──
        (
            "memory_facts",
            # Superseded facts stay for audit, but their embeddings only
            # bloat the HNSW indexes: strip vectors from rows inactive >90d.
            # embedding IS NOT NULL in the WHERE makes each pass terminate.
            {
                "days": 90,
                "timestamp_col": "updated_at",
                "batch_size": 5000,
                "action": "update",
                "set_clause": "embedding = NULL",
                "extra_where": "is_active = FALSE AND embedding IS NOT NULL",
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
    action: str = "delete",
    set_clause: str | None = None,
) -> int:
    """Apply the retention action to rows older than *days*, in batches.

    ``action="delete"`` (default) deletes expired rows; ``action="update"``
    applies *set_clause* to them instead (e.g. ``"embedding = NULL"``) — the
    clause must make the rows stop matching the WHERE so the loop terminates.
    Returns total rows affected.

    Uses a ctid-based subquery to grab a limited batch of row pointers,
    touches exactly those, and commits. Each batch holds a lock only briefly.
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Table {table!r} is not in the retention allowlist")
    if not re.fullmatch(r"[a-z_]+", timestamp_col):
        raise ValueError(f"Invalid timestamp column name: {timestamp_col!r}")
    if action not in ("delete", "update"):
        raise ValueError(f"Unknown retention action: {action!r}")
    if action == "update" and not set_clause:
        raise ValueError("action='update' requires a set_clause")
    if action == "delete" and set_clause:
        raise ValueError("set_clause is only valid with action='update'")

    from robothor.db.connection import get_connection

    where = f"{timestamp_col} < NOW() - make_interval(days => {int(days)})"
    if extra_where:
        where = f"{where} AND {extra_where}"

    if action == "update":
        statement = f"UPDATE {table} SET {set_clause} WHERE ctid = ANY("  # noqa: S608
    else:
        statement = f"DELETE FROM {table} WHERE ctid = ANY("  # noqa: S608
    statement += f"  ARRAY(SELECT ctid FROM {table} WHERE {where} LIMIT %s))"  # noqa: S608

    total = 0
    with get_connection() as conn:
        while True:
            cur = conn.cursor()
            cur.execute(statement, (batch_size,))
            batch_affected = cur.rowcount
            conn.commit()
            total += batch_affected or 0
            if batch_affected < batch_size:
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
                action=policy.get("action", "delete"),
                set_clause=policy.get("set_clause"),
            )
            results[table] = deleted
            if deleted > 0:
                # "updated" for UPDATE-based hygiene policies (e.g. stripping
                # embeddings): logging "deleted 75k rows from memory_facts"
                # for a non-destructive pass reads as memory loss.
                verb = "updated" if policy.get("action") == "update" else "deleted"
                logger.info(
                    "Retention: %s %d rows in %s (>%d days)",
                    verb,
                    deleted,
                    table,
                    policy["days"],
                )
        except Exception as e:
            logger.warning("Retention cleanup failed for %s: %s", table, e)
            results[table] = -1

    # agent_messages carries two clocks (delivered 7d, undelivered 30d with
    # per-recipient logging of dropped mail), so the messaging module owns the
    # policy and this sweep just invokes it.
    try:
        from robothor.engine.messaging import purge_old_messages

        results["agent_messages"] = purge_old_messages()
    except Exception as e:
        logger.warning("Retention cleanup failed for agent_messages: %s", e)
        results["agent_messages"] = -1

    # Files received over a channel are the one retention target that is not a
    # table. Keyed under `inbox` rather than a table name so the daemon's log
    # line reads honestly. The prune walks `<workspace>/inbox/` and nothing
    # else: a file an agent moved somewhere useful has left that tree, and
    # deleting the operator's own work would be a far worse failure than
    # keeping a copy of a screenshot too long.
    try:
        from robothor.engine.attachments import prune_inbox

        results["inbox"] = prune_inbox()
    except Exception as e:
        logger.warning("Retention cleanup failed for the channel inbox: %s", e)
        results["inbox"] = -1

    # The other file-shaped target: `analyze_image` writes a batch's full
    # per-image table to `<workspace>/.robothor/analyze_image/` whenever the
    # result is too big to return inline. One ~67 KB JSON per big batch, in a
    # directory no operator looks at, and nothing deleted them — which is what
    # a retention sweep is for. Its own window (a week) because these are
    # working files an agent reads in the run that wrote them, not the
    # operator's own data the inbox holds for a month.
    try:
        from robothor.engine.vision_batch import prune_spill_files

        results["analyze_image"] = prune_spill_files()
    except Exception as e:
        logger.warning("Retention cleanup failed for the analyze_image spill files: %s", e)
        results["analyze_image"] = -1

    # And the third: `exec` writes a command's whole stdout under
    # `<workspace>/.robothor/exec/` whenever the model's 4,000-character window
    # cannot hold it. A run reaps its own on the way out, so everything this
    # sweep finds was orphaned by a run that was killed before it could — which
    # is exactly the case a retention window exists for.
    try:
        from robothor.engine.exec_spill import prune_spill_files as prune_exec_spills

        results["exec"] = prune_exec_spills()
    except Exception as e:
        logger.warning("Retention cleanup failed for the exec spill files: %s", e)
        results["exec"] = -1
    return results

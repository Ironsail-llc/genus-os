#!/usr/bin/env python3
"""
Task Cleanup Cron for Robothor.

Runs every 4 hours (6-22h) to keep the task queue clean.
Pure Python — no AI, no LLM calls. Direct DB access.

Actions:
1. Delete test data tasks (titles matching test patterns)
2. Resolve past-date calendar conflict tasks
3. Reset stuck IN_PROGRESS tasks (>24h) back to TODO
4. Auto-resolve orphan TODO tasks (no assignee, >72h old)
5. Auto-resolve stale escalations (needs-philip >72h, [ESCALATION] >48h,
   vision >6h, self-inflicted agent tasks)
"""

import logging
import re
from datetime import UTC, datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "dbname": "robothor_memory",
    "user": "philip",
    "host": "/var/run/postgresql",
}
TENANT_ID = "robothor-primary"


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def delete_test_data(conn) -> int:
    """Delete tasks with test data titles."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE crm_tasks SET deleted_at = NOW()
        WHERE deleted_at IS NULL
          AND tenant_id = %s
          AND (
            title LIKE '__p1_verify_%%'
            OR title LIKE 'TEST %%'
            OR title ILIKE '%%smoke test%%'
          )
        """,
        (TENANT_ID,),
    )
    count = cur.rowcount
    conn.commit()
    if count:
        logger.info("Deleted %d test data tasks", count)
    return count


def resolve_past_calendar_conflicts(conn) -> int:
    """Resolve calendar conflict tasks where the date has passed."""
    now = datetime.now(UTC)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT id, title, body FROM crm_tasks
        WHERE deleted_at IS NULL
          AND resolved_at IS NULL
          AND tenant_id = %s
          AND 'calendar' = ANY(tags)
          AND 'conflict' = ANY(tags)
          AND status IN ('TODO', 'IN_PROGRESS')
        """,
        (TENANT_ID,),
    )
    rows = cur.fetchall()

    resolved = 0
    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for row in rows:
        body = row.get("body") or ""
        dates = date_re.findall(body)
        if not dates:
            continue
        # If ALL dates in the body are in the past, resolve
        all_past = True
        for d in dates:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC)
                if dt.date() >= now.date():
                    all_past = False
                    break
            except ValueError:
                all_past = False
                break
        if all_past:
            cur.execute(
                """
                UPDATE crm_tasks
                SET status = 'DONE', resolved_at = NOW(), resolution = 'Auto-resolved: calendar dates passed',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (row["id"],),
            )
            resolved += 1

    conn.commit()
    if resolved:
        logger.info("Resolved %d past-date calendar conflict tasks", resolved)
    return resolved


def reset_stuck_in_progress(conn) -> int:
    """Reset tasks stuck in IN_PROGRESS for >24h back to TODO."""
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE crm_tasks SET status = 'TODO', updated_at = NOW()
        WHERE deleted_at IS NULL
          AND tenant_id = %s
          AND status = 'IN_PROGRESS'
          AND updated_at < %s
        """,
        (TENANT_ID, cutoff),
    )
    count = cur.rowcount
    conn.commit()
    if count:
        logger.info("Reset %d stuck IN_PROGRESS tasks to TODO", count)
    return count


def resolve_orphan_todos(conn) -> int:
    """Auto-resolve unassigned TODO tasks older than 72h."""
    cutoff = datetime.now(UTC) - timedelta(hours=72)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE crm_tasks
        SET status = 'DONE', resolved_at = NOW(),
            resolution = 'Auto-resolved: unassigned >72h',
            updated_at = NOW()
        WHERE deleted_at IS NULL
          AND tenant_id = %s
          AND status = 'TODO'
          AND assigned_to_agent IS NULL
          AND created_at < %s
        """,
        (TENANT_ID, cutoff),
    )
    count = cur.rowcount
    conn.commit()
    if count:
        logger.info("Resolved %d orphan TODO tasks (>72h, unassigned)", count)
    return count


def resolve_stale_escalations(conn) -> int:
    """Auto-resolve stale escalation tasks that no longer need attention.

    Rules:
    1. Tasks with 'needs-philip' tag older than 72h
    2. Tasks with title starting with '[ESCALATION]' older than 48h
    3. Vision/unknown-person tasks older than 6h
    4. Self-inflicted agent tasks (Misconfigured, Missing Tool, Cannot retrieve)
    """
    now = datetime.now(UTC)
    cur = conn.cursor()
    total = 0

    # Rule 1: needs-philip tasks >72h
    cutoff_72h = now - timedelta(hours=72)
    cur.execute(
        """
        UPDATE crm_tasks
        SET status = 'DONE', resolved_at = NOW(),
            resolution = 'Auto-resolved: needs-philip >72h, stale',
            updated_at = NOW()
        WHERE deleted_at IS NULL
          AND resolved_at IS NULL
          AND tenant_id = %s
          AND status IN ('TODO', 'IN_PROGRESS')
          AND 'needs-philip' = ANY(tags)
          AND created_at < %s
        """,
        (TENANT_ID, cutoff_72h),
    )
    count = cur.rowcount
    total += count
    if count:
        logger.info("Resolved %d stale needs-philip tasks (>72h)", count)

    # Rule 2: [ESCALATION] tasks >48h
    cutoff_48h = now - timedelta(hours=48)
    cur.execute(
        """
        UPDATE crm_tasks
        SET status = 'DONE', resolved_at = NOW(),
            resolution = 'Auto-resolved: escalation >48h, stale',
            updated_at = NOW()
        WHERE deleted_at IS NULL
          AND resolved_at IS NULL
          AND tenant_id = %s
          AND status IN ('TODO', 'IN_PROGRESS')
          AND title LIKE '[ESCALATION]%%'
          AND created_at < %s
        """,
        (TENANT_ID, cutoff_48h),
    )
    count = cur.rowcount
    total += count
    if count:
        logger.info("Resolved %d stale [ESCALATION] tasks (>48h)", count)

    # Rule 3: Vision/unknown-person tasks >6h
    cutoff_6h = now - timedelta(hours=6)
    cur.execute(
        """
        UPDATE crm_tasks
        SET status = 'DONE', resolved_at = NOW(),
            resolution = 'Auto-resolved: vision event >6h, moment passed',
            updated_at = NOW()
        WHERE deleted_at IS NULL
          AND resolved_at IS NULL
          AND tenant_id = %s
          AND status IN ('TODO', 'IN_PROGRESS')
          AND ('vision' = ANY(tags) OR 'unknown-person' = ANY(tags))
          AND created_at < %s
        """,
        (TENANT_ID, cutoff_6h),
    )
    count = cur.rowcount
    total += count
    if count:
        logger.info("Resolved %d stale vision tasks (>6h)", count)

    # Rule 4: Self-inflicted agent tasks (any age)
    cur.execute(
        """
        UPDATE crm_tasks
        SET status = 'DONE', resolved_at = NOW(),
            resolution = 'Auto-resolved: self-inflicted agent issue, not a real escalation',
            updated_at = NOW()
        WHERE deleted_at IS NULL
          AND resolved_at IS NULL
          AND tenant_id = %s
          AND status IN ('TODO', 'IN_PROGRESS')
          AND (
            title ILIKE '%%misconfigured%%'
            OR title ILIKE '%%missing tool%%'
            OR title ILIKE '%%cannot retrieve%%'
            OR title ILIKE '%%cannot read%%'
            OR title ILIKE '%%agent: missing%%'
          )
        """,
        (TENANT_ID,),
    )
    count = cur.rowcount
    total += count
    if count:
        logger.info("Resolved %d self-inflicted agent tasks", count)

    conn.commit()
    return total


def resolve_stale_responder_tasks(conn) -> int:
    """Auto-resolve email-responder tasks >24h that match junk patterns.

    Catches receipts, security alerts, Google Cloud updates, meeting notes,
    and other non-actionable emails the classifier shouldn't have created
    tasks for. Prevents responder context window bloat.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE crm_tasks
        SET status = 'DONE', resolved_at = NOW(),
            resolution = 'Auto-resolved: junk email pattern, >24h stale',
            updated_at = NOW()
        WHERE deleted_at IS NULL
          AND resolved_at IS NULL
          AND tenant_id = %s
          AND assigned_to_agent = 'email-responder'
          AND status IN ('TODO', 'IN_PROGRESS')
          AND created_at < %s
          AND (
            title ILIKE '%%receipt%%'
            OR title ILIKE '%%order confirmation%%'
            OR title ILIKE '%%shipping%%'
            OR title ILIKE '%%delivery notification%%'
            OR title ILIKE '%%security alert%%'
            OR title ILIKE '%%sign-in%%'
            OR title ILIKE '%%signin%%'
            OR title ILIKE '%%verification code%%'
            OR title ILIKE '%%two-factor%%'
            OR title ILIKE '%%2fa%%'
            OR title ILIKE '%%google cloud%%'
            OR title ILIKE '%%billing statement%%'
            OR title ILIKE '%%payment received%%'
            OR title ILIKE '%%invoice%%'
            OR title ILIKE '%%meeting notes%%'
            OR title ILIKE '%%meeting summary%%'
            OR title ILIKE '%%calendar reminder%%'
            OR title ILIKE '%%unsubscribe%%'
            OR title ILIKE '%%newsletter%%'
            OR title ILIKE '%%no-reply%%'
            OR title ILIKE '%%noreply%%'
            OR title ILIKE '%%do not reply%%'
          )
        """,
        (TENANT_ID, cutoff),
    )
    count = cur.rowcount
    conn.commit()
    if count:
        logger.info("Resolved %d stale junk email-responder tasks (>24h)", count)
    return count


def main():
    start_time = datetime.now()
    logger.info("=== Task Cleanup Started: %s ===", start_time)

    conn = get_conn()
    try:
        test_count = delete_test_data(conn)
        calendar_count = resolve_past_calendar_conflicts(conn)
        stuck_count = reset_stuck_in_progress(conn)
        orphan_count = resolve_orphan_todos(conn)
        escalation_count = resolve_stale_escalations(conn)
        responder_count = resolve_stale_responder_tasks(conn)
    finally:
        conn.close()

    duration = (datetime.now() - start_time).total_seconds()
    total = (
        test_count
        + calendar_count
        + stuck_count
        + orphan_count
        + escalation_count
        + responder_count
    )
    logger.info("=== Task Cleanup Complete (%.1fs) ===", duration)

    print(f"Task Cleanup — {start_time.date()}")
    print(f"  Test data deleted: {test_count}")
    print(f"  Past calendar conflicts resolved: {calendar_count}")
    print(f"  Stuck IN_PROGRESS reset: {stuck_count}")
    print(f"  Orphan TODOs resolved: {orphan_count}")
    print(f"  Stale escalations resolved: {escalation_count}")
    print(f"  Junk responder tasks resolved: {responder_count}")
    print(f"  Total actions: {total}")
    print(f"  Duration: {duration:.1f}s")


if __name__ == "__main__":
    main()

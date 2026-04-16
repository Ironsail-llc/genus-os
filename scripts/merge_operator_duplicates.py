#!/usr/bin/env python3
"""Transactional cleanup for operator duplicate rows and garbage CRM contacts.

Run *after* reviewing ``scripts/audit_philip_contacts.py``. Nothing is
inferred — every action takes explicit IDs. The operator decides each row.

Usage:
    # Preview only, rolls back at the end:
    python scripts/merge_operator_duplicates.py \\
        --canonical <UUID> \\
        --merge <UUID>,<UUID> \\
        --delete <UUID> \\
        --dry-run

    # Apply (prompts once for confirmation unless --yes):
    python scripts/merge_operator_duplicates.py \\
        --tenant robothor-primary \\
        --canonical <UUID> \\
        --merge <UUID>,<UUID> \\
        --delete <UUID> \\
        --yes

Per merge target:
    contact_identifiers.person_id, crm_tasks.person_id, crm_notes.person_id,
    crm_conversations.person_id, crm_routines.person_id — all repointed to
    the canonical row. The loser is soft-deleted (``deleted_at = NOW()``).

Per delete target:
    Must have zero non-null FK references remaining (fail loud if not).
    Hard-deletes the row. Use only for unambiguous garbage (e.g., rows
    auto-created from Drive share notifications).

Everything runs inside one transaction per invocation; any error rolls back.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger("merge_operator_duplicates")


MERGE_FK_TABLES = [
    ("contact_identifiers", "person_id"),
    ("crm_tasks", "person_id"),
    ("crm_notes", "person_id"),
    ("crm_conversations", "person_id"),
    ("crm_routines", "person_id"),
]


def _parse_uuid_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def _assert_exists(cur, person_id: str, tenant_id: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT id, first_name, last_name, email, deleted_at
        FROM crm_people
        WHERE id = %s AND tenant_id = %s
        """,
        (person_id, tenant_id),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"ERROR: no crm_people row with id={person_id} in tenant={tenant_id}")
    return dict(row)


def _repoint_fks(cur, loser: str, canonical: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, col in MERGE_FK_TABLES:
        cur.execute(
            f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
            (canonical, loser),
        )
        counts[table] = cur.rowcount
    return counts


def _count_refs(cur, person_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, col in MERGE_FK_TABLES:
        cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} = %s",
            (person_id,),
        )
        counts[table] = cur.fetchone()[0]
    return counts


def _soft_delete(cur, person_id: str) -> int:
    cur.execute(
        """
        UPDATE crm_people
        SET deleted_at = NOW(), updated_at = NOW()
        WHERE id = %s AND deleted_at IS NULL
        """,
        (person_id,),
    )
    return cur.rowcount


def _hard_delete(cur, person_id: str) -> int:
    cur.execute("DELETE FROM crm_people WHERE id = %s", (person_id,))
    return cur.rowcount


def _log_audit(cur, op: str, canonical: str, target: str, details: dict) -> None:
    try:
        cur.execute(
            """
            INSERT INTO audit_log (operation, entity_type, entity_id, details, created_at)
            VALUES (%s, %s, %s, %s::jsonb, NOW())
            """,
            (
                f"merge_person:{op}",
                "person",
                target,
                __import__("json").dumps({"canonical": canonical, **details}),
            ),
        )
    except Exception:
        logger.debug("audit_log insert failed (non-fatal)", exc_info=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--canonical", required=True, help="UUID of the row to keep")
    ap.add_argument("--merge", default="", help="Comma-separated UUIDs to merge into canonical")
    ap.add_argument("--delete", default="", help="Comma-separated UUIDs to hard-delete")
    ap.add_argument("--dry-run", action="store_true", help="Roll back at the end")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    merge_ids = _parse_uuid_list(args.merge)
    delete_ids = _parse_uuid_list(args.delete)
    if not merge_ids and not delete_ids:
        print("ERROR: nothing to do — provide --merge and/or --delete", file=sys.stderr)
        return 2

    overlap = set(merge_ids) & set(delete_ids) | {args.canonical} & (
        set(merge_ids) | set(delete_ids)
    )
    if overlap:
        print(f"ERROR: canonical/merge/delete sets overlap: {overlap}", file=sys.stderr)
        return 2

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        canonical = _assert_exists(cur, args.canonical, args.tenant)
        if canonical["deleted_at"] is not None:
            print("ERROR: canonical row is soft-deleted — pick a live row", file=sys.stderr)
            return 2

        print(
            f"Canonical: {canonical['first_name']} {canonical['last_name']} "
            f"<{canonical['email']}> ({args.canonical})"
        )
        print(f"Merge ({len(merge_ids)}): {merge_ids or '—'}")
        print(f"Delete ({len(delete_ids)}): {delete_ids or '—'}")

        if not args.yes and not args.dry_run:
            resp = input("\nProceed? [y/N] ").strip().lower()
            if resp != "y":
                print("Aborted.")
                return 1

        # Merge phase
        for loser in merge_ids:
            _assert_exists(cur, loser, args.tenant)
            counts = _repoint_fks(cur, loser, args.canonical)
            soft = _soft_delete(cur, loser)
            print(f"  merged {loser[:8]}: {counts} soft_deleted={soft}")
            _log_audit(cur, "merge", args.canonical, loser, counts)

        # Delete phase
        for target in delete_ids:
            _assert_exists(cur, target, args.tenant)
            refs = _count_refs(cur, target)
            total_refs = sum(refs.values())
            if total_refs > 0:
                print(
                    f"\nERROR: cannot delete {target}: {total_refs} FK references remain ({refs}).",
                    file=sys.stderr,
                )
                if args.dry_run:
                    print("  (dry-run continuing; would fail on apply)")
                else:
                    conn.rollback()
                    return 3
            hard = _hard_delete(cur, target)
            print(f"  hard-deleted {target[:8]}: removed={hard}")
            _log_audit(cur, "delete", args.canonical, target, {})

        if args.dry_run:
            conn.rollback()
            print("\n[dry-run] Rolled back. No changes persisted.")
        else:
            conn.commit()
            print("\nCommitted.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

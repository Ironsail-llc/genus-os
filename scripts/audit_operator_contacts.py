#!/usr/bin/env python3
"""Audit ``crm_people`` rows matching a first name.

Read-only. Produces a Markdown table the operator can review row-by-row
before running ``scripts/merge_operator_duplicates.py``. The suggested
actions are advisory — the operator decides each row's fate explicitly.

Usage:
    python scripts/audit_operator_contacts.py              # uses owner's first name
    python scripts/audit_operator_contacts.py --name alice
    python scripts/audit_operator_contacts.py --tenant default

Columns:
    id, name, emails, identifiers, tasks, notes, conversations,
    memory_refs, provenance, suggested_action
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.owner_config import load_owner_config

# Heuristics for "this email is clearly a system-sender, not a person".
# Substring patterns (lowercased) — matched against the full email string.
# Deliberately generic: covers Drive share notifications, calendar
# auto-senders, and other no-reply templates across providers.
GARBAGE_EMAIL_PATTERNS = (
    "noreply",
    "no-reply",
    "drive-shares",
    "calendar-notification",
    "mailer-daemon",
)


def _collect_rows(cur, tenant_id: str, name_query: str) -> list[dict[str, Any]]:
    pattern = f"%{name_query}%"
    cur.execute(
        """
        SELECT p.id, p.first_name, p.last_name, p.email, p.additional_emails,
               p.phone, p.created_at, p.updated_at, p.tenant_id,
               (SELECT COUNT(*) FROM contact_identifiers WHERE person_id = p.id) AS n_identifiers,
               (SELECT COUNT(*) FROM crm_tasks
                    WHERE person_id = p.id AND deleted_at IS NULL) AS n_tasks,
               (SELECT COUNT(*) FROM crm_notes
                    WHERE person_id = p.id AND deleted_at IS NULL) AS n_notes,
               (SELECT COUNT(*) FROM crm_conversations
                    WHERE person_id = p.id) AS n_conversations
        FROM crm_people p
        WHERE p.deleted_at IS NULL
          AND p.tenant_id = %s
          AND (p.first_name ILIKE %s OR p.last_name ILIKE %s)
        ORDER BY p.created_at ASC
        """,
        (tenant_id, pattern, pattern),
    )
    return [dict(r) for r in cur.fetchall()]


def _identifiers_for(cur, person_id: str) -> list[str]:
    cur.execute(
        """
        SELECT channel, identifier, display_name
        FROM contact_identifiers
        WHERE person_id = %s
        ORDER BY created_at ASC
        """,
        (person_id,),
    )
    return [f"{r['channel']}:{(r['identifier'] or '')[:32]}" for r in cur.fetchall()]


def _memory_refs(cur, name_query: str, tenant_id: str) -> dict[str, int]:
    """Best-effort count of memory references that mention this name."""
    cur.execute(
        """
        SELECT COUNT(*) AS n FROM memory_entities
        WHERE tenant_id = %s AND name ILIKE %s
        """,
        (tenant_id, f"%{name_query}%"),
    )
    entities = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT COUNT(*) AS n FROM memory_facts
        WHERE tenant_id = %s AND is_active = TRUE
          AND fact_text ILIKE %s
        """,
        (tenant_id, f"%{name_query}%"),
    )
    facts = cur.fetchone()["n"]
    return {"entities": entities, "facts": facts}


def _suggest(row: dict[str, Any], owner_email: str | None) -> str:
    email = (row.get("email") or "").strip().lower()
    if owner_email and email == owner_email:
        return "likely-owner-duplicate"
    if email and any(pat in email for pat in GARBAGE_EMAIL_PATTERNS):
        return "likely-garbage"
    return "keep"


def _provenance(identifiers: list[str], email: str | None) -> str:
    if email:
        return email
    if identifiers:
        return identifiers[0]
    return "—"


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    header = (
        "| id (short) | name | email | identifiers | tasks | notes | convos | "
        "mem (entities/facts) | provenance | suggestion |"
    )
    sep = "|" + "---|" * 10
    out = [header, sep]
    for r in rows:
        short_id = str(r["id"])[:8]
        name = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "—"
        email = r["email"] or "—"
        idents = ", ".join(r["_identifiers"]) or "—"
        mem = f"{r['_memory']['entities']}/{r['_memory']['facts']}"
        out.append(
            f"| `{short_id}` | {name} | {email} | {idents} | "
            f"{r['n_tasks']} | {r['n_notes']} | {r['n_conversations']} | "
            f"{mem} | {r['_provenance']} | **{r['_suggestion']}** |"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--name", default=None, help="First/last name substring (default: owner first_name)"
    )
    ap.add_argument(
        "--tenant", default=None, help="Tenant ID (default: owner.yaml tenant or DEFAULT_TENANT)"
    )
    ap.add_argument("--full-ids", action="store_true", help="Print full UUIDs (for merge script)")
    args = ap.parse_args()

    owner_cfg = load_owner_config()
    tenant_id = args.tenant or (owner_cfg.tenant_id if owner_cfg else DEFAULT_TENANT)
    name_query = args.name or (owner_cfg.first_name if owner_cfg else "")
    if not name_query:
        print("ERROR: no --name given and no owner.yaml found.", file=sys.stderr)
        return 2

    owner_email = owner_cfg.email if owner_cfg else None

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        rows = _collect_rows(cur, tenant_id, name_query)
        mem = _memory_refs(cur, name_query, tenant_id)

        for r in rows:
            r["_identifiers"] = _identifiers_for(cur, r["id"])
            r["_provenance"] = _provenance(r["_identifiers"], r["email"])
            r["_suggestion"] = _suggest(r, owner_email)
            r["_memory"] = mem

    print(f"# Audit — `{name_query}` in tenant `{tenant_id}`\n")
    if owner_cfg:
        print(f"Owner config: `{owner_cfg.email}` ({owner_cfg.full_name})\n")
    if not rows:
        print("_No matching rows._\n")
        return 0

    print(_markdown_table(rows))
    print()
    print(
        f"**Total rows:** {len(rows)}  |  "
        f"**memory entities matching '{name_query}':** {mem['entities']}  |  "
        f"**memory facts mentioning '{name_query}':** {mem['facts']}\n"
    )

    if args.full_ids:
        print("## Full IDs (for merge script):\n")
        for r in rows:
            print(
                f"- `{r['id']}` — {r['first_name']} {r['last_name']} ({r['email'] or 'no-email'})"
            )

    print(
        "\n> Suggestions are advisory. Review each row and choose an action:\n"
        "> `keep`, `merge` into the canonical operator row, or `delete`.\n"
        "> Then invoke `scripts/merge_operator_duplicates.py`."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

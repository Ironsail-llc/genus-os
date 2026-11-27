"""Make old shared conversation facts as private as the conversations they came from.

Before conversation facts took their conversation's scope, conversation ingest
wrote every fact ``shared``: what a person said in a one-to-one chat reached
every reader in the tenant. Conversation ingest does not re-extract that
history (it starts each conversation where the old ingest left it), so those
facts stay shared until this job is run. Nothing runs it automatically: it
changes who can read existing memory, which is the operator's decision.

What it does, per tenant, for each shared fact the old ingest wrote
(``source_type = 'conversation'`` with a ``session_key`` in its metadata):

* finds the conversation's owner the way its segments are scoped
  (``segments.session_scope``);
* a one-to-one conversation with a known owner: the fact moves to that owner's
  scope (``user`` / ``person:{id}`` or ``user:{uid}``), with the scoped content
  hash; a copy the owner already holds retires it instead;
* a workspace room, a conversation with no known owner, or one whose session is
  gone: left shared, and counted by reason.

Dry run by default: it reports what it would change and changes nothing.
Applied, it works in batches by fact id and remembers where it stopped
(``ingested_items``, ``conversation_facts_backfill``), so it can be stopped and
run again; ``restart=True`` starts over. Edges already drawn from a re-scoped
fact are not removed: the report counts them (``edges_from_rescoped``).

    python -m robothor.memory.conversation_backfill --tenant <id>            # dry run
    python -m robothor.memory.conversation_backfill --tenant <id> --apply
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from typing import Any

from robothor.db.connection import get_connection, tenant_scope

logger = logging.getLogger(__name__)

_CURSOR_SOURCE = "conversation_facts_backfill"
DEFAULT_BATCH = 500


def _owner(tenant_id: str, session_key: str, sessions: dict[str, Any]) -> tuple[str | None, str]:
    """``(scope_id, reason)``: the conversation's owner, or why it has none."""
    from robothor.memory.segments import session_scope
    from robothor.rooms.keys import parse_room_key

    if parse_room_key(session_key) is not None:
        return None, "room"
    if session_key not in sessions:
        return None, "no_session"
    kind, sid = session_scope(tenant_id, session_key, sessions[session_key])
    if kind != "user" or not sid:
        return None, "no_owner"
    return sid, "owner"


def rescope_conversation_facts(
    tenant_id: str, *, apply: bool = False, batch: int = DEFAULT_BATCH, restart: bool = False
) -> dict[str, Any]:
    """Report (dry run) or apply the re-scoping of one tenant's old conversation facts."""
    with tenant_scope(tenant_id), get_connection() as conn:
        cur = conn.cursor()
        after = 0
        if apply and not restart:
            cur.execute(
                "SELECT content_hash FROM ingested_items WHERE tenant_id = %s "
                "AND source_name = %s AND item_id = %s",
                (tenant_id, _CURSOR_SOURCE, tenant_id),
            )
            row = cur.fetchone()
            after = int(row[0]) if row and str(row[0]).isdigit() else 0
        cur.execute(
            "SELECT id, metadata->>'session_key' FROM memory_facts WHERE tenant_id = %s "
            "AND scope_kind = 'shared' AND source_type = 'conversation' "
            "AND metadata ? 'session_key' AND id > %s ORDER BY id" + (" LIMIT %s" if apply else ""),
            (tenant_id, after, batch + 1) if apply else (tenant_id, after),
        )
        rows = list(cur.fetchall())
        more = apply and len(rows) > batch
        rows = rows[:batch] if apply else rows
        keys = sorted({str(k) for _id, k in rows})
        cur.execute(
            "SELECT session_key, person_id::text FROM chat_sessions "
            "WHERE tenant_id = %s AND session_key = ANY(%s)",
            (tenant_id, keys),
        )
        sessions = dict(cur.fetchall())
        owners = {k: _owner(tenant_id, k, sessions) for k in keys}
        by_owner: Counter[str] = Counter()
        left: Counter[str] = Counter()
        targets: dict[str, list[int]] = {}
        for fact_id, key in rows:
            sid, reason = owners[str(key)]
            if sid is None:
                left[reason] += 1
                continue
            by_owner[sid] += 1
            targets.setdefault(sid, []).append(int(fact_id))
        report: dict[str, Any] = {
            "tenant_id": tenant_id,
            "dry_run": not apply,
            "examined": len(rows),
            "would_rescope": sum(by_owner.values()),
            "by_owner": dict(by_owner),
            "left_shared": dict(left),
            "more": more,
        }
        ids = [i for group in targets.values() for i in group]
        cur.execute(
            "SELECT count(*) FROM memory_relation_evidence WHERE tenant_id = %s "
            "AND fact_id = ANY(%s)",
            (tenant_id, ids),
        )
        report["edges_from_rescoped"] = int(cur.fetchone()[0])
        if not apply:
            conn.rollback()
            return report
        rescoped = 0
        for sid, group in targets.items():
            rescoped += _rescope(cur, tenant_id, sid, group)
        if rows:
            cur.execute(
                "INSERT INTO ingested_items (tenant_id, source_name, item_id, content_hash, "
                "fact_ids, ingested_at) VALUES (%s, %s, %s, %s, '{}', NOW()) "
                "ON CONFLICT (tenant_id, source_name, item_id) DO UPDATE "
                "SET content_hash = EXCLUDED.content_hash, ingested_at = NOW()",
                (tenant_id, _CURSOR_SOURCE, tenant_id, str(int(rows[-1][0]))),
            )
        conn.commit()
    report["rescoped"] = rescoped
    logger.info("conversation backfill for %s: %d facts re-scoped", tenant_id, rescoped)
    return report


def _rescope(cur: Any, tenant_id: str, scope_id: str, fact_ids: list[int]) -> int:
    """Move shared facts to ``user`` / ``scope_id``; a copy the owner holds retires one."""
    scoped_hash = (
        "encode(sha256(convert_to(COALESCE({p}fact_text, '') || '|' || COALESCE({p}tenant_id, '') "
        "|| '|' || COALESCE({p}category, '') || '|scope:user:' || %s, 'UTF8')), 'hex')"
    )
    person = scope_id.split(":", 1)[1] if scope_id.startswith("person:") else ""
    cur.execute(
        "UPDATE memory_facts f SET is_active = FALSE, superseded_by = k.id FROM memory_facts k "
        "WHERE f.tenant_id = %s AND f.id = ANY(%s) AND f.is_active AND k.tenant_id = f.tenant_id "
        "AND k.is_active AND k.id <> f.id AND k.content_hash = " + scoped_hash.format(p="f."),
        (tenant_id, fact_ids, scope_id),
    )
    cur.execute(
        "UPDATE memory_facts SET scope_kind = 'user', scope_id = %s, content_hash = "
        + scoped_hash.format(p="")
        + ", person_id = (SELECT p.id FROM crm_people p WHERE p.id::text = %s "
        "AND p.tenant_id = %s) WHERE tenant_id = %s AND id = ANY(%s) AND scope_kind = 'shared'",
        (scope_id, scope_id, person, tenant_id, tenant_id, fact_ids),
    )
    return int(cur.rowcount)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m robothor.memory.conversation_backfill",
        description="Re-scope old shared conversation facts to their conversation's owner.",
    )
    parser.add_argument("--tenant", required=True, help="the tenant to work on")
    parser.add_argument("--apply", action="store_true", help="change rows (default: dry run)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="facts per run")
    parser.add_argument("--restart", action="store_true", help="ignore where it stopped")
    args = parser.parse_args(argv)
    report = rescope_conversation_facts(
        args.tenant, apply=args.apply, batch=args.batch, restart=args.restart
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

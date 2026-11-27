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
* facts that meet in one scope with the same text keep the oldest (the others
  retire, never a duplicate key);
* a workspace room, a conversation with no known owner, or one whose session is
  gone: left shared, and counted by reason;
* a row consolidation built from moved facts moves too when all its members
  sit in one owner's scope (followed through consolidations of consolidations);
  one built from mixed members stays shared and is counted
  (``consolidated_left_shared``);
* each re-scoped fact leaves the shared graph, as a private write never enters
  it: its evidence rows go and its relations are kept on it
  (``metadata.graph_pending_relations``); an edge left with no evidence is
  deleted; a node nothing else names (not CRM-linked, no contact identifier,
  no edge, no other fact's mention of any owner, no shared fact's ``entities``)
  is removed with these facts' mentions of it and its decisions; nodes kept
  because another fact still names them are counted.

One run works through every batch until no old fact is left. Dry run by
default: it does all of the work in one transaction and rolls it back, so its
report (facts, duplicates, edges, nodes) is what applying would change, and it
can be repeated. Applied, it commits batch by batch and remembers where it
stopped (``ingested_items``, ``conversation_facts_backfill``), so an
interrupted run resumes; ``restart=True`` starts over.

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
    tenant_id: str,
    *,
    apply: bool = False,
    batch: int = DEFAULT_BATCH,
    restart: bool = False,
    max_batches: int | None = None,
) -> dict[str, Any]:
    """Report (dry run) or apply the re-scoping of one tenant's old conversation facts.

    Works through every batch of ``batch`` facts until none is left (or
    ``max_batches`` have run). Applied, each batch commits and moves the saved
    cursor, so an interrupted run resumes where it stopped. A dry run does
    every batch in one transaction and rolls it all back, cursor included: its
    totals are what applying would change, and it can be repeated.
    """
    totals: dict[str, Any] = {
        "examined": 0,
        "rescoped": 0,
        "retired_duplicates": 0,
        "consolidated_rescoped": 0,
        "consolidated_left_shared": 0,
    }
    by_owner: Counter[str] = Counter()
    left: Counter[str] = Counter()
    graph_totals: Counter[str] = Counter()
    batches = 0
    more = False
    with tenant_scope(tenant_id), get_connection() as conn:
        cur = conn.cursor()
        after = 0 if restart else _saved_cursor(cur, tenant_id)
        while True:
            part = _one_batch(cur, tenant_id, after, batch)
            batches += 1
            totals["examined"] += part["examined"]
            totals["rescoped"] += part["rescoped"]
            totals["retired_duplicates"] += part["retired"]
            totals["consolidated_rescoped"] += part["consolidated_rescoped"]
            totals["consolidated_left_shared"] += part["consolidated_left_shared"]
            by_owner.update(part["by_owner"])
            left.update(part["left"])
            graph_totals.update(part["graph"])
            more = part["more"]
            if part["last_id"] is not None:
                after = part["last_id"]
            if apply:
                conn.commit()
            if not more or (max_batches is not None and batches >= max_batches):
                break
        if not apply:
            conn.rollback()
    report: dict[str, Any] = {
        "tenant_id": tenant_id,
        "dry_run": not apply,
        "batches": batches,
        "examined": totals["examined"],
        "would_rescope": sum(by_owner.values()),
        "by_owner": dict(by_owner),
        "left_shared": dict(left),
        "more": more,
        "rescoped": totals["rescoped"] if apply else 0,
        "retired_duplicates": totals["retired_duplicates"],
        "consolidated_rescoped": totals["consolidated_rescoped"],
        "consolidated_left_shared": totals["consolidated_left_shared"],
        **{k: graph_totals.get(k, 0) for k in _GRAPH_KEYS},
    }
    logger.info(
        "conversation backfill for %s (%s): %d batches, %d re-scoped, %d duplicates retired, "
        "%d edges and %d nodes removed",
        tenant_id,
        "applied" if apply else "dry run",
        batches,
        totals["rescoped"],
        totals["retired_duplicates"],
        report["edges_removed"],
        report["nodes_removed"],
    )
    return report


def _saved_cursor(cur: Any, tenant_id: str) -> int:
    cur.execute(
        "SELECT content_hash FROM ingested_items WHERE tenant_id = %s "
        "AND source_name = %s AND item_id = %s",
        (tenant_id, _CURSOR_SOURCE, tenant_id),
    )
    row = cur.fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else 0


def _one_batch(cur: Any, tenant_id: str, after: int, batch: int) -> dict[str, Any]:
    """Re-scope the next ``batch`` old conversation facts past ``after``."""
    cur.execute(
        "SELECT id, metadata->>'session_key' FROM memory_facts WHERE tenant_id = %s "
        "AND scope_kind = 'shared' AND source_type = 'conversation' "
        "AND metadata ? 'session_key' AND id > %s ORDER BY id LIMIT %s",
        (tenant_id, after, batch + 1),
    )
    rows = list(cur.fetchall())
    more = len(rows) > batch
    rows = rows[:batch]
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
    rescoped = retired = 0
    for sid, group in targets.items():
        moved, dropped = _rescope(cur, tenant_id, sid, group)
        rescoped, retired = rescoped + moved, retired + dropped
    moved_ids = [i for g in targets.values() for i in g]
    graph = Counter(_unpublish_graph(cur, tenant_id, moved_ids))
    consolidated = _follow_consolidations(cur, tenant_id, moved_ids)
    graph.update(consolidated.pop("graph"))
    last_id = int(rows[-1][0]) if rows else None
    if last_id is not None:
        cur.execute(
            "INSERT INTO ingested_items (tenant_id, source_name, item_id, content_hash, "
            "fact_ids, ingested_at) VALUES (%s, %s, %s, %s, '{}', NOW()) "
            "ON CONFLICT (tenant_id, source_name, item_id) DO UPDATE "
            "SET content_hash = EXCLUDED.content_hash, ingested_at = NOW()",
            (tenant_id, _CURSOR_SOURCE, tenant_id, str(last_id)),
        )
    return {
        "examined": len(rows),
        "more": more,
        "last_id": last_id,
        "by_owner": by_owner,
        "left": left,
        "rescoped": rescoped,
        "retired": retired,
        "graph": graph,
        **consolidated,
    }


def _follow_consolidations(cur: Any, tenant_id: str, moved: list[int]) -> dict[str, Any]:
    """Re-scope what consolidation built from facts this batch moved.

    Consolidation merges similar shared facts into a new shared row and retires
    the members into it (``superseded_by``), so a moved fact's content may live
    on, shared, in that row. Following ``superseded_by`` outward (a
    consolidated row may itself have been consolidated): a row whose members
    all sit in one owner's scope moves to it, and leaves the graph, like any
    moved fact; one whose members are mixed stays shared. Counts the readable
    (active) rows of each kind.
    """
    out: dict[str, Any] = {
        "consolidated_rescoped": 0,
        "consolidated_left_shared": 0,
        "graph": Counter(),
    }
    seen, frontier = set(moved), list(moved)
    while frontier:
        cur.execute(
            "SELECT DISTINCT superseded_by FROM memory_facts WHERE tenant_id = %s "
            "AND id = ANY(%s) AND superseded_by IS NOT NULL",
            (tenant_id, frontier),
        )
        successors = [int(r[0]) for r in cur.fetchall() if int(r[0]) not in seen]
        seen.update(successors)
        frontier = []
        for row_id in successors:
            cur.execute(
                "SELECT scope_kind, is_active FROM memory_facts WHERE id = %s AND tenant_id = %s",
                (row_id, tenant_id),
            )
            found = cur.fetchone()
            if found is None:
                continue
            kind, active = found
            if kind != "shared":
                frontier.append(row_id)  # already private: look further along
                continue
            cur.execute(
                "SELECT DISTINCT scope_kind, scope_id FROM memory_facts "
                "WHERE tenant_id = %s AND superseded_by = %s",
                (tenant_id, row_id),
            )
            scopes = {(k, sid) for k, sid in cur.fetchall()}
            if len(scopes) == 1 and next(iter(scopes))[0] == "user":
                ((_kind, sid),) = scopes
                _rescope(cur, tenant_id, str(sid), [row_id])
                out["graph"].update(_unpublish_graph(cur, tenant_id, [row_id]))
                out["consolidated_rescoped"] += 1 if active else 0
                frontier.append(row_id)
            elif active:
                out["consolidated_left_shared"] += 1
    return out


_GRAPH_KEYS = (
    "evidence_removed",
    "edges_removed",
    "nodes_removed",
    "nodes_kept_by_other_facts",
    "nodes_kept_by_entities",
)


_SCOPED_HASH = (
    "encode(sha256(convert_to(COALESCE({p}fact_text, '') || '|' || COALESCE({p}tenant_id, '') "
    "|| '|' || COALESCE({p}category, '') || '|scope:user:' || %s, 'UTF8')), 'hex')"
)


def _rescope(cur: Any, tenant_id: str, scope_id: str, fact_ids: list[int]) -> tuple[int, int]:
    """Move shared facts to ``user`` / ``scope_id``: ``(re-scoped, retired)``.

    Facts that would share one scoped hash (the same text and category, which
    shared rows kept apart by their person) keep the oldest; a copy the owner
    already holds retires the incoming one. Neither is ever a duplicate key.
    """
    cur.execute(
        "SELECT id, " + _SCOPED_HASH.format(p="") + " FROM memory_facts "
        "WHERE tenant_id = %s AND id = ANY(%s) AND is_active ORDER BY id",
        (scope_id, tenant_id, fact_ids),
    )
    keeper: dict[str, int] = {}
    retire: list[tuple[int, int]] = []
    for fact_id, digest in cur.fetchall():
        if digest in keeper:
            retire.append((int(fact_id), keeper[digest]))
        else:
            keeper[digest] = int(fact_id)
    if keeper:
        cur.execute(
            "SELECT content_hash, id FROM memory_facts WHERE tenant_id = %s AND is_active "
            "AND content_hash = ANY(%s) AND NOT (id = ANY(%s))",
            (tenant_id, list(keeper), fact_ids),
        )
        for digest, held in cur.fetchall():
            retire.append((keeper.pop(digest), int(held)))
    for fact_id, kept in retire:
        cur.execute(
            "UPDATE memory_facts SET is_active = FALSE, superseded_by = %s "
            "WHERE id = %s AND tenant_id = %s",
            (kept, fact_id, tenant_id),
        )
    person = scope_id.split(":", 1)[1] if scope_id.startswith("person:") else ""
    cur.execute(
        "UPDATE memory_facts SET scope_kind = 'user', scope_id = %s, content_hash = "
        + _SCOPED_HASH.format(p="")
        + ", person_id = (SELECT p.id FROM crm_people p WHERE p.id::text = %s "
        "AND p.tenant_id = %s) WHERE tenant_id = %s AND id = ANY(%s) AND scope_kind = 'shared'",
        (scope_id, scope_id, person, tenant_id, tenant_id, fact_ids),
    )
    return len(keeper), len(retire)


def _unpublish_graph(cur: Any, tenant_id: str, fact_ids: list[int]) -> dict[str, int]:
    """Take re-scoped facts out of the shared graph, as a private write never enters it.

    Each fact's evidence rows go and its relations are parked on it
    (``metadata.graph_pending_relations``, as a private write keeps them); an
    edge left with no evidence is deleted, and a kept edge's origin is
    recomputed. A node is removed when nothing but these facts names it: not
    linked to the CRM or a contact identifier, no edge, no mention from any
    other fact (shared, or any person's private one), and no active shared
    fact naming it in its ``entities`` column. Nodes kept for those last two
    reasons are counted (``nodes_kept_by_other_facts``,
    ``nodes_kept_by_entities``). A removed node's mentions by these facts and
    its resolution decisions go with it (a private write records an unresolved
    name nowhere but on the fact).
    """
    from robothor.memory.write_path import recompute_edge_origins

    out = {
        "evidence_removed": 0,
        "edges_removed": 0,
        "nodes_removed": 0,
        "nodes_kept_by_other_facts": 0,
        "nodes_kept_by_entities": 0,
    }
    if not fact_ids:
        return out
    cur.execute(
        "SELECT e.fact_id, e.relation_id, s.name, r.relation_type, t.name "
        "FROM memory_relation_evidence e JOIN memory_relations r ON r.id = e.relation_id "
        "JOIN memory_entities s ON s.id = r.source_entity_id "
        "JOIN memory_entities t ON t.id = r.target_entity_id "
        "WHERE e.tenant_id = %s AND e.fact_id = ANY(%s) ORDER BY e.fact_id, e.relation_id",
        (tenant_id, fact_ids),
    )
    pending: dict[int, list[dict[str, str]]] = {}
    relation_ids: set[int] = set()
    for fact_id, rel_id, source, relation, target in cur.fetchall():
        pending.setdefault(int(fact_id), []).append(
            {"source": source, "relation": relation, "target": target}
        )
        relation_ids.add(int(rel_id))
    for fact_id, rels in pending.items():
        cur.execute(
            "UPDATE memory_facts SET metadata = COALESCE(metadata, '{}'::jsonb) || "
            "jsonb_build_object('graph_pending_relations', %s::jsonb) "
            "WHERE id = %s AND tenant_id = %s",
            (json.dumps(rels), fact_id, tenant_id),
        )
    cur.execute(
        "DELETE FROM memory_relation_evidence WHERE tenant_id = %s AND fact_id = ANY(%s)",
        (tenant_id, fact_ids),
    )
    out["evidence_removed"] = int(cur.rowcount)
    ids = sorted(relation_ids)
    cur.execute(
        "DELETE FROM memory_relations r WHERE r.tenant_id = %s AND r.id = ANY(%s) "
        "AND NOT EXISTS (SELECT 1 FROM memory_relation_evidence e WHERE e.relation_id = r.id) "
        "RETURNING source_entity_id, target_entity_id",
        (tenant_id, ids),
    )
    endpoints: set[int] = set()
    for source, target in cur.fetchall():
        endpoints.update((int(source), int(target)))
        out["edges_removed"] += 1
    cur.execute(
        "UPDATE memory_relations r SET fact_id = (SELECT min(e.fact_id) FROM "
        "memory_relation_evidence e WHERE e.relation_id = r.id) "
        "WHERE r.tenant_id = %s AND r.id = ANY(%s) AND r.fact_id = ANY(%s)",
        (tenant_id, ids, fact_ids),
    )
    recompute_edge_origins(cur, tenant_id, ids)
    cur.execute(
        "SELECT DISTINCT entity_id FROM memory_mentions WHERE tenant_id = %s "
        "AND fact_id = ANY(%s) AND entity_id IS NOT NULL",
        (tenant_id, fact_ids),
    )
    candidates = sorted(endpoints | {int(r[0]) for r in cur.fetchall()})
    cur.execute(
        "SELECT n.id, "
        "EXISTS (SELECT 1 FROM memory_mentions m WHERE m.entity_id = n.id "
        "AND NOT (m.fact_id = ANY(%s))), "
        "EXISTS (SELECT 1 FROM memory_facts f WHERE f.tenant_id = n.tenant_id "
        "AND f.is_active AND f.scope_kind = 'shared' AND NOT (f.id = ANY(%s)) "
        "AND lower(n.name) = ANY(SELECT lower(x) FROM unnest(f.entities) x)) "
        "FROM memory_entities n WHERE n.tenant_id = %s AND n.id = ANY(%s) "
        "AND n.crm_person_id IS NULL AND n.crm_company_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM contact_identifiers c WHERE c.memory_entity_id = n.id) "
        "AND NOT EXISTS (SELECT 1 FROM memory_relations r "
        "WHERE r.source_entity_id = n.id OR r.target_entity_id = n.id)",
        (fact_ids, fact_ids, tenant_id, candidates),
    )
    gone: list[int] = []
    for node, other_facts, named in cur.fetchall():
        if other_facts:
            # Another fact (shared, or any person's private one) still resolves
            # to it: left as it is, and reported.
            out["nodes_kept_by_other_facts"] += 1
        elif named:
            # A shared fact names it only in its entities column (written
            # before mentions existed): still a shared name.
            out["nodes_kept_by_entities"] += 1
        else:
            gone.append(int(node))
    if gone:
        cur.execute(
            "DELETE FROM memory_mentions WHERE entity_id = ANY(%s) AND fact_id = ANY(%s)",
            (gone, fact_ids),
        )
        cur.execute(
            "DELETE FROM memory_resolution_decisions WHERE resolved_entity_id = ANY(%s)", (gone,)
        )
        cur.execute(
            "DELETE FROM memory_entities WHERE tenant_id = %s AND id = ANY(%s)", (tenant_id, gone)
        )
        out["nodes_removed"] = len(gone)
    return out


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

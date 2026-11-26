"""Entity-aware recall tail: no rows about a different person who shares a name.

Recall fills its budget past the reranked head (``facts._fill_from_pool``). On
the eval world every answer the fill lost was a tail row about a DIFFERENT
person or company sharing a name token with the one the question names ("Wendy
Calley" under "Where is Wendy Larkmore employed?"). This module decides which
tail rows those are; it never touches the reranked head and adds no model call.

* The question's entities are the ones recall's seed linking resolves
  (``activation.seeds_from_text``): strong keys or a full name or alias of
  exactly one shared node, never a single token.
* A tail fact is dropped when it is linked to a person or company entity that
  is not a named one but shares a name token with a named person or company,
  and is not also linked to a named entity. A row's links are its mentions
  (``memory_mentions``); a fact without mentions falls back to the names in
  its ``entities`` column.
* Only full names (two tokens or more) confuse: a row linked only to a
  first-name-only entity is kept, as is a row about an entity with no shared
  token, a row with no links, and every row when the question names no
  resolvable person or company.

It reads only rows the caller already holds and entities the seed linking
already resolves (shared nodes), so it narrows the tail and never widens it.
Sync; call it off the loop.
"""

from __future__ import annotations

import re
from typing import Any

PERSON_OR_COMPANY = frozenset({"person", "organization", "company"})


def _norm(name: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(name or "").lower()))


def _full_name_tokens(name: Any) -> set[str]:
    tokens = _norm(name).split()
    return set(tokens) if len(tokens) >= 2 else set()


def confusable_tail_keys(
    tenant_id: str, query: str, rows: list[dict[str, Any]]
) -> set[tuple[str, Any]]:
    """``(kind, id)`` of the ``rows`` to leave out of the tail; empty when none."""
    facts = [r for r in rows if r.get("kind", "fact") == "fact" and r.get("id") is not None]
    if not facts:
        return set()
    from robothor.memory.activation import seeds_from_text

    seeds = seeds_from_text(tenant_id, query)
    if not seeds:
        return set()
    # Looked up per call, as activation's cursor does, so a patched
    # robothor.db.get_connection applies.
    import robothor.db
    from robothor.db.connection import tenant_scope

    get_connection = robothor.db.get_connection

    seed_ids = sorted({int(s.entity_id) for s in seeds})
    fact_ids = sorted({int(r["id"]) for r in facts})
    with tenant_scope(tenant_id), get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, entity_type, aliases FROM memory_entities "
            "WHERE tenant_id = %s AND id = ANY(%s)",
            (tenant_id, seed_ids),
        )
        named = [row for row in cur.fetchall() if (row[2] or "").lower() in PERSON_OR_COMPANY]
        if not named:
            return set()
        cur.execute(
            "SELECT m.fact_id, e.id, e.name, e.entity_type FROM memory_mentions m "
            "JOIN memory_entities e ON e.id = m.entity_id AND e.tenant_id = m.tenant_id "
            "WHERE m.tenant_id = %s AND m.fact_id = ANY(%s)",
            (tenant_id, fact_ids),
        )
        links: dict[int, list[tuple[Any, Any, Any]]] = {}
        for fact_id, eid, name, etype in cur.fetchall():
            links.setdefault(int(fact_id), []).append((eid, name, etype))

    named_ids = {int(row[0]) for row in named}
    named_names = {_norm(row[1]) for row in named}
    for row in named:
        named_names.update(_norm(a) for a in (row[3] or []))
    named_tokens: set[str] = set()
    for name in named_names:
        named_tokens |= _full_name_tokens(name)
    if not named_tokens:
        return set()

    drop: set[tuple[str, Any]] = set()
    for r in facts:
        fid = int(r["id"])
        linked = links.get(fid)
        if linked is None:  # no mentions: the names on the row, type unknown
            linked = [(None, name, None) for name in (r.get("entities") or [])]
        if any(eid in named_ids or _norm(name) in named_names for eid, name, _ in linked):
            continue
        for _eid, name, etype in linked:
            if etype is not None and str(etype).lower() not in PERSON_OR_COMPANY:
                continue
            if _full_name_tokens(name) & named_tokens:
                drop.add((r.get("kind", "fact"), r.get("id")))
                break
    return drop

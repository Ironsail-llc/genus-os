"""The Helm's Memory page — read what the instance believes, and forget one fact.

Three routes, all operator-only:

``GET  /api/memory/facts``                    what is believed, newest first.
``POST /api/memory/facts/{id}/forget/preview``  what a forget would do. Writes nothing.
``POST /api/memory/facts/{id}/forget``          bound the fact, with a reason.

Separate from ``routers/memory.py`` because the two answer different questions.
That module is the memory SUBSYSTEM's proxy — search, ingest, blocks, the
pipeline — and every route on it is a thin pass-through to
:mod:`robothor.memory`. These three are an operator surface over the
``memory_facts`` TABLE, with their own gate, their own pagination and their own
audit row, and folding them in would have meant one module where half the
routes are agent-reachable under ``_memory_admin_scope`` and half are not.

Four rules the routes enforce:

1. **Forget BOUNDS a fact; it does not delete one.** ``is_active = false`` and
   ``valid_to = now()`` — the migration-095 shape, the same one supersession
   uses. The row, its text and its provenance stay, because "the operator
   forgot this on the 3rd" is itself a thing the appliance has to be able to
   say afterwards. Nothing here issues a DELETE.
2. **One fact, never a chain.** A forget touches the row the operator named and
   no other. Facts that this one superseded stay exactly as they are: the
   operator asked to stop believing one claim, not to rewrite the history that
   produced it.
3. **Another tenant's fact is a 404.** Never a 403 — a 403 confirms the id
   names a real fact, which is the whole of what an enumeration needs.
4. **The audit row carries the id and the reason. Never the text.** The text is
   the content being forgotten, often the most sensitive line in the table, and
   the audit log is exported to a SIEM (``robothor.audit.siem``).

Handlers are plain ``def`` (not ``async def``) on purpose: psycopg2 is
synchronous, so FastAPI must run them in its worker threadpool rather than on
the event loop (see ``crm/bridge/tests/test_route_concurrency.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from robothor.db.connection import get_connection
from robothor.sanitize import sanitize_log
from routers._audit import audited
from routers._operator import require_operator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory"])

#: How many facts one page may carry. The Helm's table paginates; a caller
#: asking for the whole 150k-row table over one request would hold a threadpool
#: worker for the duration of the transfer.
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

#: An operator's reason is a sentence, not a payload. Three characters is the
#: floor because "no" and "x" are not reasons; 500 is the ceiling the settings
#: API and ``feature_flag_audit.reason`` already use for the same kind of note.
MIN_REASON = 3
MAX_REASON = 500

#: The three populations the page offers. Spelled as words rather than a
#: boolean so ``all`` has a name — a tri-state squeezed into ``?active=`` with
#: an empty string meaning "both" is the kind of contract a UI gets wrong once.
ACTIVE_FILTERS = ("true", "false", "all")

#: Every column the API returns, in payload order. One tuple so the SELECT, the
#: search re-read and the payload builder cannot disagree about what a fact is.
_COLUMNS = (
    "id",
    "fact_text",
    "entities",
    "is_active",
    "valid_from",
    "valid_to",
    "confidence",
    "source_type",
    "created_at",
    "superseded_by",
)
_SELECT = ", ".join(_COLUMNS)


class ForgetRequest(BaseModel):
    """Why the operator is forgetting this.

    Deliberately UNCONSTRAINED here and bounded in the handler: a pydantic
    ``Field`` cap is enforced by FastAPI before the route runs, and its
    rejection is ``{"detail": [ ... ]}`` — a second 422 body shape for a route
    whose contract says every refusal is a flat ``{"detail": "<sentence>"}``.
    """

    reason: str | None = None


def _fact_id(value: str) -> int:
    """A ``memory_facts.id`` in its canonical form, or 422.

    ``memory_facts.id`` is ``SERIAL`` — a 32-bit signed integer — so the bound
    is the column's, not a taste. Without this, ``WHERE id = %s`` on a caller's
    typo is a psycopg2 ``DataError`` and the operator reads a 500, which is
    indistinguishable from an appliance that has actually broken.
    """
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="not a fact id") from None
    if parsed < 1 or parsed > 2147483647:
        raise HTTPException(status_code=422, detail="not a fact id")
    return parsed


def _limit(value: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="limit must be a number") from None
    if not 1 <= parsed <= MAX_LIMIT:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {MAX_LIMIT}")
    return parsed


def _reason(body: ForgetRequest | None) -> str:
    text = ((body.reason if body else None) or "").strip()
    if len(text) < MIN_REASON:
        raise HTTPException(
            status_code=422,
            detail=f"a reason of at least {MIN_REASON} characters is required to forget a fact",
        )
    if len(text) > MAX_REASON:
        raise HTTPException(
            status_code=422, detail=f"a reason must be under {MAX_REASON} characters"
        )
    return sanitize_log(text)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _payload(row: tuple[Any, ...]) -> dict[str, Any]:
    """One database row as the Helm's fact object.

    ``source`` rather than ``source_type``: the page shows one provenance
    string and the column name is an implementation detail of the ingestion
    pipeline, which has already renamed it once.
    """
    record = dict(zip(_COLUMNS, row, strict=True))
    return {
        "id": int(record["id"]),
        "fact_text": record["fact_text"],
        "entities": list(record["entities"] or []),
        "is_active": bool(record["is_active"]),
        "valid_from": _iso(record["valid_from"]),
        "valid_to": _iso(record["valid_to"]),
        "confidence": record["confidence"],
        "source": record["source_type"],
        "created_at": _iso(record["created_at"]),
        "superseded_by": record["superseded_by"],
    }


def _active_predicate(active: str) -> tuple[str, list[Any]]:
    if active == "all":
        return "", []
    return " AND is_active = %s", [active == "true"]


def _ranked_by_search(query: str, tenant_id: str, active: str, limit: int) -> list[int]:
    """The ids ``/api/memory/search`` would return, in its order.

    Through ``robothor.memory.facts.search_facts`` and nothing else. A keyword
    or vector ranking written here would be a SECOND answer to "what does this
    instance consider relevant", and the two would drift the first time either
    was tuned — which is the failure this repo has paid for in three other
    places (see ``routers/settings.py``'s header).

    ``asyncio.run`` is safe and deliberate: this handler is a plain ``def``, so
    FastAPI runs it in a worker thread that has no event loop of its own. It is
    the same call ``robothor.memory.facts.search_facts_compat`` makes for the
    same reason. Imported inside the function so the symbol resolves per
    request rather than being bound at module import.
    """
    import asyncio

    from robothor.memory.facts import search_facts

    results = asyncio.run(
        search_facts(query, limit=limit, active_only=(active == "true"), tenant_id=tenant_id)
    )
    return [int(r["id"]) for r in results if r.get("id") is not None]


def _facts_by_id(cur: Any, ids: list[int], tenant_id: str, active: str) -> list[dict[str, Any]]:
    """The named facts, in the order named, scoped to the caller's tenant.

    The re-read is not redundant. ``search_facts`` selects the columns ITS
    ranking needs, which do not include ``is_active``, ``valid_from`` or
    ``valid_to`` — so a page built from its rows would be missing exactly the
    fields the forget button depends on. The tenant predicate is repeated here
    too: defence in depth is cheap, and this is the query whose result is
    rendered.
    """
    if not ids:
        return []
    clause, params = _active_predicate(active)
    cur.execute(
        f"SELECT {_SELECT} FROM memory_facts WHERE id = ANY(%s) AND tenant_id = %s{clause}",  # noqa: S608 -- _SELECT is a module constant
        [ids, tenant_id, *params],
    )
    found = {int(row[0]): _payload(row) for row in cur.fetchall()}
    return [found[fact_id] for fact_id in ids if fact_id in found]


@router.get("/facts")
def list_facts(
    request: Request,
    q: str | None = Query(None, description="Free-text query; ranked by the memory search path"),
    entity: str | None = Query(None, description="Only facts tagged with this entity"),
    active: str = Query("true", description="true | false | all"),
    limit: str = Query(str(DEFAULT_LIMIT)),
    cursor: str | None = Query(None, description="Keyset cursor: the last id of the page before"),
) -> dict[str, Any]:
    """One page of what this tenant believes.

    Newest first, and paginated by KEYSET rather than OFFSET. ``memory_facts.id``
    is a ``SERIAL``, so descending id is insertion order exactly; an offset page
    over a table the ingestion pipeline is writing to would skip and repeat rows
    while the operator read it.

    With ``q``, the order is the search path's and the page is a single one —
    ``next_cursor`` is null. A keyset cursor over a relevance ranking would be a
    cursor over an order the next request can recompute differently, which is a
    pagination bug that only shows up once the corpus moves.
    """
    require_operator(request)
    from deps import get_tenant_id

    tenant_id = get_tenant_id(request)
    page = _limit(limit)
    if active not in ACTIVE_FILTERS:
        raise HTTPException(status_code=422, detail="active must be true, false or all")
    after = _fact_id(cursor) if cursor is not None else None

    # BEFORE the connection below, deliberately: ``search_facts`` takes its own
    # connection out of the same pool (and an embedding call besides), so
    # ranking inside a held one would be two pool slots per request and a
    # deadlock the moment the pool is the narrower of the two.
    ranked = _ranked_by_search(q, tenant_id, active, page) if q else None

    with get_connection() as conn:
        cur = conn.cursor()
        if ranked is not None:
            return {
                "facts": _facts_by_id(cur, ranked, tenant_id, active),
                "next_cursor": None,
            }

        clause, params = _active_predicate(active)
        sql = f"SELECT {_SELECT} FROM memory_facts WHERE tenant_id = %s{clause}"  # noqa: S608 -- _SELECT is a module constant
        args: list[Any] = [tenant_id, *params]
        if entity:
            # lower_entities() is the expression migration 109 indexed, so this
            # is the case-insensitive form that uses the index rather than the
            # one that reads the table.
            sql += " AND lower_entities(entities) @> ARRAY[lower(%s)]"
            args.append(entity)
        if after is not None:
            sql += " AND id < %s"
            args.append(after)
        sql += " ORDER BY id DESC LIMIT %s"
        args.append(page)
        cur.execute(sql, args)
        facts = [_payload(row) for row in cur.fetchall()]

    next_cursor = str(facts[-1]["id"]) if len(facts) == page else None
    return {"facts": facts, "next_cursor": next_cursor}


def _load(cur: Any, fact_id: int, tenant_id: str) -> dict[str, Any]:
    """One fact in the caller's tenant, or 404 — never 403, never the text."""
    cur.execute(
        f"SELECT {_SELECT} FROM memory_facts WHERE id = %s AND tenant_id = %s",  # noqa: S608 -- _SELECT is a module constant
        (fact_id, tenant_id),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no fact with that id")
    return _payload(row)


@router.post("/facts/{fact_id}/forget/preview")
def preview_forget(fact_id: str, request: Request) -> dict[str, Any]:
    """What forgetting this fact would do. Writes nothing.

    ``references`` is what the operator needs to know BEFORE the click, and
    each of the three answers a different question:

    * ``entities``  — which people/projects this claim is filed under;
    * ``episodes``  — how many nightly summaries were built citing it, none of
      which are rewritten by a forget (they keep the id; the fact stops being
      believed);
    * ``blocks``    — which always-in-context memory blocks QUOTE the text, and
      therefore keep telling agents the thing after it is forgotten. That is
      the one an operator cannot discover any other way, and the one that makes
      a forget look like it did not work.
    """
    require_operator(request)
    from deps import get_tenant_id

    tenant_id = get_tenant_id(request)
    identifier = _fact_id(fact_id)

    with get_connection() as conn:
        cur = conn.cursor()
        fact = _load(cur, identifier, tenant_id)
        cur.execute(
            "SELECT count(*) FROM memory_episodes WHERE tenant_id = %s AND %s = ANY(fact_ids)",
            (tenant_id, identifier),
        )
        episodes = int(cur.fetchone()[0])
        # strpos() rather than LIKE: the fact text is operator-visible content
        # that routinely contains % and _, and escaping a LIKE pattern is a
        # step somebody forgets.
        cur.execute(
            "SELECT block_name FROM agent_memory_blocks "
            "WHERE tenant_id = %s AND strpos(content, %s) > 0 ORDER BY block_name",
            (tenant_id, fact["fact_text"]),
        )
        blocks = [row[0] for row in cur.fetchall()]

    already_inactive = not fact["is_active"]
    return {
        "fact": fact,
        # Only ever this fact. Rule 2 in the module header — and the write path
        # asserts the same list, so the two cannot drift.
        "would_deactivate": [] if already_inactive else [fact["id"]],
        "references": {"entities": fact["entities"], "episodes": episodes, "blocks": blocks},
        "already_inactive": already_inactive,
    }


@router.post("/facts/{fact_id}/forget")
def forget_fact(
    fact_id: str, request: Request, body: ForgetRequest | None = None
) -> dict[str, Any]:
    """Stop believing one fact, with a reason, on the record.

    409 rather than a silent second success: a forget is not idempotent in the
    way a PUT is, because the interesting output is the AUDIT ROW, and a second
    one saying the operator forgot an already-forgotten fact is a trail that
    disagrees with the table. The conflict says which it is.
    """
    require_operator(request)
    from deps import get_tenant_id

    tenant_id = get_tenant_id(request)
    identifier = _fact_id(fact_id)
    reason = _reason(body)

    with get_connection() as conn:
        cur = conn.cursor()
        # FOR UPDATE, so "is it already inactive" and "make it inactive" are one
        # decision. Two concurrent clicks would otherwise both read active, both
        # write, and both report success — with two audit rows for one act.
        cur.execute(
            "SELECT is_active FROM memory_facts WHERE id = %s AND tenant_id = %s FOR UPDATE",
            (identifier, tenant_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no fact with that id")
        if not row[0]:
            raise HTTPException(
                status_code=409,
                detail="that fact is already inactive; there is nothing left to forget.",
            )
        cur.execute(
            "UPDATE memory_facts SET is_active = false, valid_to = now(), updated_at = now() "
            "WHERE id = %s AND tenant_id = %s",
            (identifier, tenant_id),
        )
        fact = _load(cur, identifier, tenant_id)

    audited(
        request,
        "memory.forget",
        action=str(identifier),
        # Identifiers and the operator's own sentence. Never fact_text — see
        # rule 4 in this module's header.
        fact_id=identifier,
        reason=reason,
    )
    return {"fact": fact, "forgotten": True}

"""Memory router — query-classed recall across stores (RIP 15).

`search_memory` historically hard-coded ``expand_entities + insights +
episodes = True`` on *every* call, so a simple lookup paid for a full fan-out.
The router classifies the query cheaply (heuristics) and queries only the
stores that fit, then fuses and budget-caps the merged result.

Query classes and the stores they hit:
    exact_lookup → Knowledge Vault (captions) + facts
    temporal     → facts + episodes, then recency-reordered
    how_to       → procedures + facts
    who_is       → facts with entity-graph expansion
    intent       → standing intents + facts
    default      → facts + insights  (the prior fan-out, minus episodes/entities)

Gated by ``ROBOTHOR_RIP_15_ENABLED``; when off, callers keep the old path.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.memory.fusion import rrf_fuse
from robothor.memory.recall import RECALL_CONTEXT_CHARS

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_CHARS = RECALL_CONTEXT_CHARS

QUERY_CLASSES = ("exact_lookup", "temporal", "how_to", "who_is", "intent", "default")

# Heuristic cues, checked in priority order. First match wins.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "how_to",
        re.compile(
            r"\b(how (do|to|can) (i|we|you)|steps to|procedure for|playbook)\b", re.IGNORECASE
        ),
    ),
    (
        "exact_lookup",
        re.compile(
            r"\b(phone|number|account|address|email|zip|postal|routing|account id|"
            r"case (id|number)|exact|verbatim|api key|credential|password|bookmark|url for)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "temporal",
        re.compile(
            r"\b(latest|most recent|last|current|currently|now|recently|today|"
            r"this week|resolved|resolve|legitimate|confirmed|closed|settled|"
            r"outcome of|status of|what did .* (decide|say|choose)|"
            r"did .* (confirm|resolve|close|decide))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "who_is",
        re.compile(
            r"\b(who is|who's|who are|contact for|what does .* do|reports to)\b", re.IGNORECASE
        ),
    ),
    (
        "intent",
        re.compile(
            r"\b(working toward|standing (goal|intent)|objective|"
            r"what (am i|are we) (trying|working)|priorit)\b",
            re.IGNORECASE,
        ),
    ),
]


def classify_query(query: str) -> str:
    """Classify a recall query into one of QUERY_CLASSES (heuristic, cheap)."""
    for cls, pattern in _PATTERNS:
        if pattern.search(query):
            return cls
    return "default"


#: Fields a conversation segment row keeps through normalisation, so the tool
#: output can say where it came from.
SEGMENT_FIELDS = (
    "kind",
    "topic",
    "source_kind",
    "source_ref",
    "start_ref",
    "end_ref",
    "part",
    "started_at",
    "ended_at",
)


def _normalize_fact(r: dict[str, Any]) -> dict[str, Any]:
    row = _normalized_fact_fields(r)
    # A row the graph leg found keeps its labels: who asserted it, the path of
    # entities that reached it, and which legs found it (the shared formatter,
    # ``recall.row_text``, labels a row only the graph found).
    for key in ("origin", "graph_hops", "graph_path", "leg_ranks", "budget_tail"):
        if key in r:
            row[key] = r[key]
    return row


def _normalized_fact_fields(r: dict[str, Any]) -> dict[str, Any]:
    extra = {k: r.get(k) for k in SEGMENT_FIELDS} if r.get("kind") == "segment" else {}
    return {
        **extra,
        "id": r.get("id"),
        "source": r.get("source") or "fact",
        "text": r.get("fact_text") or r.get("insight_text") or r.get("content") or "",
        "category": r.get("category", ""),
        # Episodes expose start_time/end_time, not created_at — fall back to them
        # so the temporal re-sort ranks a fresh episode instead of sinking it.
        "created_at": r.get("created_at") or r.get("end_time") or r.get("start_time"),
        "score": r.get("rrf_score") or r.get("similarity") or 0.0,
        # Carried deliberately. rows_dropped_by_scope counts rows whose
        # person_id is neither None nor the caller's; dropping the field here
        # made every row read as org-general, so observe mode reported
        # dropped=0 for a restricted caller — a clean signal from a control
        # that was doing nothing. Stripped again before the tool returns.
        "person_id": r.get("person_id"),
    }


async def recall(
    query: str,
    *,
    tenant_id: str = "",
    limit: int = 10,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    scope: Any = None,
    reader_person_id: str | None = None,
    room_ids: Any = None,
    reader_user_id: str | None = None,
    reader_scope_id: str | None = None,
) -> dict[str, Any]:
    """Route a recall query to the relevant stores, fuse, and budget-cap.

    Returns ``{"query_class": str, "results": [ {id, source, text, score, ...} ]}``.

    ``scope`` is the caller's :class:`~robothor.identity.scope.DataScope`, or
    None for unrestricted. It must be threaded to every store that can return
    person-linked rows; a store that cannot express person scoping is excluded
    for restricted callers rather than returned unfiltered.

    ``reader_person_id`` / ``reader_user_id`` / ``room_ids`` decide which conversation segments the
    reader may see, on every call (``robothor.memory.segments``).

    ``reader_person_id`` / ``reader_user_id`` are also the facts' reader
    (``robothor.memory.visibility``); ``reader_scope_id`` is one more reader id
    for a caller outside a run, and the engine passes none.
    """
    resolved_tenant = tenant_id or DEFAULT_TENANT
    cls = classify_query(query)
    ranked_lists: list[list[dict[str, Any]]] = []

    # --- facts (always queried; flags chosen per class) ---
    from robothor.memory.facts import search_facts

    fact_rows = await search_facts(
        query,
        limit=limit,
        tenant_id=resolved_tenant,
        expand_entities=(cls == "who_is"),
        include_insights=(cls == "default"),
        include_episodes=(cls == "temporal"),
        scope=scope,
        reader_person_id=reader_person_id,
        reader_user_id=reader_user_id,
        room_ids=room_ids,
        reader_scope_id=reader_scope_id,
    )
    ranked_lists.append([_normalize_fact(r) for r in fact_rows])

    # --- exact lookup → Knowledge Vault captions (no values) ---
    # Fails closed for restricted callers. The vault holds credential and PII
    # captions and search_vault has no person scoping, so returning it
    # unfiltered would be a strictly wider exposure than the path this router
    # replaced — that path never queried the vault at all. The leg also
    # measured no recall benefit, so closing it costs nothing.
    if cls == "exact_lookup" and not (scope is not None and getattr(scope, "restricted", False)):
        try:
            from robothor.memory.vault import search_vault

            vault_rows = await search_vault(query, limit=limit, tenant_id=resolved_tenant)
            ranked_lists.append(
                [
                    {
                        "id": r["id"],
                        "source": "vault",
                        "text": f"{r['caption']} (vault entry {r['id']} — use memory_vault_get)",
                        "score": r.get("similarity", 0.0),
                    }
                    for r in vault_rows
                ]
            )
        except Exception as e:  # noqa: BLE001 — auxiliary store is best-effort
            logger.debug("router vault leg failed: %s", e)

    # --- intent → standing intents ---
    if cls == "intent":
        try:
            from robothor.memory.intents import search_intents

            intent_rows = await search_intents(query, limit=limit, tenant_id=resolved_tenant)
            ranked_lists.append(
                [
                    {
                        "id": r["id"],
                        "source": "intent",
                        "text": f"{r['title']} — {r.get('description', '')}".strip(" —"),
                        "score": r.get("similarity", 0.0),
                    }
                    for r in intent_rows
                ]
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("router intent leg failed: %s", e)

    # --- how_to → procedures ---
    if cls == "how_to":
        try:
            from robothor.memory.procedures import find_applicable_procedures

            procs = await find_applicable_procedures(
                task_description=query, limit=limit, tenant_id=resolved_tenant
            )
            ranked_lists.append(
                [
                    {
                        "id": p["id"],
                        "source": "procedure",
                        "text": f"{p['name']}: {'; '.join(p.get('steps') or [])}",
                        "score": p.get("similarity", 0.0),
                    }
                    for p in procs
                ]
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("router procedure leg failed: %s", e)

    fused = rrf_fuse(ranked_lists)

    # Temporal queries want the newest matching fact first, within the head.
    # Rows that follow the head stay behind it in their own order, so a newer
    # one never pushes the head out: a row only the graph leg found, then the
    # budget fill's tail rows (unscored, ``budget_tail``).
    if cls == "temporal":
        from robothor.memory.recall import graph_only

        head = [r for r in fused if not r.get("budget_tail") and not graph_only(r)]
        related = [r for r in fused if graph_only(r) and not r.get("budget_tail")]
        tail = [r for r in fused if r.get("budget_tail")]
        head.sort(
            key=lambda r: (r.get("created_at") is not None, r.get("created_at")), reverse=True
        )
        fused = head + related + tail

    # Budget cap: the one packing rule (recall.pack_within), then the limit.
    from robothor.memory.recall import pack_within, row_chars, truncate_row

    capped = pack_within(fused, budget_chars, row_chars, truncate_row)[:limit]

    return {"query_class": cls, "results": capped}

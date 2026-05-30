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

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_CHARS = 4000

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


def _normalize_fact(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r.get("id"),
        "source": r.get("source") or "fact",
        "text": r.get("fact_text") or r.get("insight_text") or r.get("content") or "",
        "category": r.get("category", ""),
        # Episodes expose start_time/end_time, not created_at — fall back to them
        # so the temporal re-sort ranks a fresh episode instead of sinking it.
        "created_at": r.get("created_at") or r.get("end_time") or r.get("start_time"),
        "score": r.get("rrf_score") or r.get("similarity") or 0.0,
    }


async def recall(
    query: str,
    *,
    tenant_id: str = "",
    limit: int = 10,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
) -> dict[str, Any]:
    """Route a recall query to the relevant stores, fuse, and budget-cap.

    Returns ``{"query_class": str, "results": [ {id, source, text, score, ...} ]}``.
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
    )
    ranked_lists.append([_normalize_fact(r) for r in fact_rows])

    # --- exact lookup → Knowledge Vault captions (no values) ---
    if cls == "exact_lookup":
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

    # Temporal queries want the newest matching fact first.
    if cls == "temporal":
        fused.sort(
            key=lambda r: (r.get("created_at") is not None, r.get("created_at")), reverse=True
        )

    # Budget cap: stop once the accumulated text would exceed budget_chars.
    capped: list[dict[str, Any]] = []
    used = 0
    for r in fused:
        if len(capped) >= limit:
            break
        used += len(r.get("text") or "")
        if capped and used > budget_chars:
            break
        capped.append(r)

    return {"query_class": cls, "results": capped}

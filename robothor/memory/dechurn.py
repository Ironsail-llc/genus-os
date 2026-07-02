"""One-time, reversible de-churn of the live memory_facts table (WS-8).

The forward fixes (consolidation guard, write-dedup, briefing skip) stop NEW
churn; this collapses ACTIVE redundancy that already accumulated. All changes are
``is_active`` flips — no hard deletes — and the deactivated ids are returned so
the operator can revert with a single ``UPDATE ... SET is_active = TRUE``.

Most dead weight is ALREADY inactive (superseded) and is filtered by
``active_only`` at retrieval, so it does not hurt recall and is left alone. This
targets only ACTIVE near-duplicates: facts that share an entity and are >=90%
token-identical (reworded restatements of the same thing). Dry-run by default.

Run:
    python -c "from robothor.memory.dechurn import dechurn; import json; \
        print(json.dumps(dechurn(dry_run=True), indent=2, default=str))"
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.memory.facts import _norm_tokens

logger = logging.getLogger(__name__)

_DEFAULT_JACCARD = 0.9


def cluster_near_dup_losers(
    facts: list[dict[str, Any]], *, jaccard: float = _DEFAULT_JACCARD
) -> list[int]:
    """Pure: ids to DEACTIVATE among active near-duplicates (keep the newest).

    ``facts`` items need ``id``, ``fact_text``, ``entities``. Two facts are
    near-duplicates when they share an entity and their normalized-token Jaccard
    overlap is >= ``jaccard``. The older (lower id) of each near-dup pair is a
    loser; the newest survivor in any cluster is never dropped.
    """
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for f in facts:
        for e in f.get("entities") or []:
            by_entity.setdefault(e, []).append(f)

    tokens: dict[int, frozenset[str]] = {}
    losers: set[int] = set()
    for bucket in by_entity.values():
        ids = [f["id"] for f in bucket]
        for f in bucket:
            if f["id"] not in tokens:
                tokens[f["id"]] = _norm_tokens(f.get("fact_text", ""))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                ta, tb = tokens[a], tokens[b]
                if not ta or not tb:
                    continue
                union = len(ta | tb)
                jac = len(ta & tb) / union if union else 0.0
                if jac >= jaccard:
                    losers.add(min(a, b))  # keep the newer (higher id)
    return sorted(losers)


def _load_active_facts(cur: Any, tenant: str) -> list[dict[str, Any]]:
    from psycopg2.extras import RealDictCursor

    cur2 = cur.connection.cursor(cursor_factory=RealDictCursor)
    cur2.execute(
        "SELECT id, fact_text, entities FROM memory_facts "
        "WHERE is_active = TRUE AND tenant_id = %s",
        (tenant,),
    )
    return [dict(r) for r in cur2.fetchall()]


def dechurn(
    tenant_id: str = "", *, dry_run: bool = True, jaccard: float = _DEFAULT_JACCARD
) -> dict[str, Any]:
    """Collapse active near-duplicate facts. Dry-run by default.

    Returns a report; when ``dry_run`` is False, deactivates the loser ids and
    includes them in ``deactivated_ids`` (the restore manifest).
    """
    tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        facts = _load_active_facts(cur, tenant)
        losers = cluster_near_dup_losers(facts, jaccard=jaccard)
        report: dict[str, Any] = {
            "tenant": tenant,
            "active_facts": len(facts),
            "near_dup_losers": len(losers),
            "jaccard": jaccard,
            "dry_run": dry_run,
        }
        if not dry_run and losers:
            cur.execute(
                "UPDATE memory_facts SET is_active = FALSE, updated_at = NOW() "
                "WHERE id = ANY(%s) AND tenant_id = %s",
                (losers, tenant),
            )
            report["deactivated"] = cur.rowcount
            report["deactivated_ids"] = losers  # restore manifest
            logger.info("dechurn: deactivated %d active near-duplicate facts", cur.rowcount)
    return report

"""Resolution capture — record that an open item / alert / decision is closed.

Memory reliably stored *detection* events (re-extracted many times across the
agent's repeated briefings) but never captured the operator's *resolution*, so
recall returned the stale "open / needs review" framing for hours until an
ordinary re-extraction happened to phrase it as closed. This module gives the
agent a direct path: write a discrete, high-importance resolution fact AND
(when enabled) retire the matching open fact(s) so the current state wins.

The resolution fact is always stored (purely additive). The riskier mutation —
superseding existing open facts — is gated behind ``MEMORY_RESOLUTION_CAPTURE``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.memory.confirmed import is_operator_confirmed
from robothor.memory.conflicts import _supersede_fact, find_similar_facts
from robothor.memory.graph_schema import Origin
from robothor.memory.write_path import WriteContext, write_facts

logger = logging.getLogger(__name__)

_RESOLUTION_IMPORTANCE = 0.85
_SUPERSEDE_SIMILARITY = 0.7
_MAX_SUPERSEDE = 3


def _resolution_capture_enabled() -> bool:
    """Gate the auto-supersede of open facts. Default OFF (observe).

    With the flag off, record_resolution still STORES the resolution fact (safe,
    additive); only the retirement of matching open facts is withheld.
    """
    raw = os.environ.get("MEMORY_RESOLUTION_CAPTURE", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def compose_resolution_text(open_item: str, outcome: str, confirmed_by: str = "") -> str:
    """Compose the crisp resolution fact text (pure, unit-testable)."""
    base = f"{open_item.strip()} — resolved: {outcome.strip()}"
    if confirmed_by.strip():
        base += f" (confirmed by {confirmed_by.strip()})"
    return base


async def record_resolution(
    open_item: str,
    outcome: str,
    *,
    confirmed_by: str = "",
    tenant_id: str = "",
    agent_id: str = "unknown",
) -> dict[str, Any]:
    """Store a resolution fact and retire the matching open fact(s).

    Returns ``{"resolution_id", "superseded_ids", "fact"}`` or ``{"error": ...}``.
    """
    tenant = tenant_id or DEFAULT_TENANT
    if not (open_item or "").strip() or not (outcome or "").strip():
        return {"error": "open_item and outcome are required"}

    text = compose_resolution_text(open_item, outcome, confirmed_by)
    fact = {"fact_text": text, "category": "resolution", "entities": [], "confidence": 1.0}
    # Through the one write path: the fact is the agent's statement (agent-
    # generated, whoever it says confirmed it), shared like the open items it
    # closes, and its mentions are the known names it contains. Conflict
    # handling is this function's own (the gated retirement below).
    context = WriteContext(
        tenant_id=tenant,
        source_channel="",
        content_type="resolution",
        origin=Origin.AGENT_GENERATED,
    )
    written = await write_facts(
        [fact],
        source_content=f"[resolution recorded by {agent_id}]",
        context=context,
        conflict_check=False,
    )
    new_id = (written.get("fact_ids") or [0])[0]
    if not new_id:
        # Nothing is kept when the quality gate refuses the fact; reporting
        # that as a resolution would hide it.
        return {"error": "resolution not stored: the memory quality gate refused it"}

    # store_fact stores the default importance (0.5); a confirmed resolution is
    # high-signal — lift it so recency+importance float it above the open noise.
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE memory_facts SET importance_score = %s WHERE id = %s AND tenant_id = %s",
            (_RESOLUTION_IMPORTANCE, new_id, tenant),
        )

    superseded: list[int] = []
    if _resolution_capture_enabled():
        # Shared open items only: a resolution recorded in the shared scope never
        # retires anyone's private fact.
        matches = await find_similar_facts(
            open_item,
            limit=_MAX_SUPERSEDE,
            threshold=_SUPERSEDE_SIMILARITY,
            tenant_id=tenant,
            scope_kind="shared",
        )
        for m in matches:
            # An operator-confirmed fact is the operator's word, not an open
            # item the pipeline may close.
            if m["id"] == new_id or is_operator_confirmed(m):
                continue
            _supersede_fact(m["id"], new_id, tenant_id=tenant)
            superseded.append(m["id"])

    logger.info(
        "record_resolution: stored fact %d, retired %d open fact(s)", new_id, len(superseded)
    )
    return {"resolution_id": new_id, "superseded_ids": superseded, "fact": text}

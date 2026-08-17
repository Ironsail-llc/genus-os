"""Bi-temporal validity for memory facts (migration 095).

Two time axes, not one:

    created_at / updated_at   when we LEARNED it
    valid_from / valid_to     when it was TRUE in the world

``conflicts.resolve_and_store`` currently answers both "update" and
"contradiction" by deactivating the old row. Those are different events —
an update means the world changed and the old fact was true until it wasn't;
a contradiction means one of the two claims is simply wrong — and with a single
time axis there is no way to say so. The cost is concrete: an agent asked "what
did we decide last month" cannot answer, because last month's truth was deleted
rather than bounded.

Nothing here changes retrieval until ``MEMORY_BITEMPORAL`` is on. The columns
are written regardless (they are additive and cost nothing), so that by the time
the flag flips there is coverage to flip onto — a point-in-time filter over a
table of NULLs would answer every historical question with silence.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

# Classifications that mean "the world changed" rather than "someone was wrong".
TEMPORAL_CLASSIFICATIONS = frozenset({"update"})

# Every classification the conflict resolver can emit, including the two that
# do not supersede. Recorded so the denominator exists: an error rate needs the
# cases where nothing happened as much as the ones where something did.
KNOWN_CLASSIFICATIONS = frozenset({"new", "duplicate", "update", "contradiction", "reinforced"})


def bitemporal_enabled() -> bool:
    """Point-in-time filtering on the read path. Default OFF.

    Off does not mean inert: valid_from/valid_to are still written, because a
    filter switched on over a table of NULLs answers every historical question
    with silence and would be indistinguishable from a broken feature.
    """
    return os.environ.get("MEMORY_BITEMPORAL", "").strip().lower() in ("1", "true", "on", "yes")


def point_in_time_predicate(as_of: datetime | None) -> tuple[str, list[Any]]:
    """SQL fragment selecting facts that were true at ``as_of``.

    NULL on either bound means "unbounded", NOT "invalid". This is the whole
    ballgame for a table where 152k pre-existing rows have both columns NULL:
    if NULL read as "not yet valid", turning this on would blank the entire
    backlog and look like catastrophic recall loss.

    Returns ``("", [])`` when ``as_of`` is None so callers can concatenate
    unconditionally.
    """
    if as_of is None:
        return "", []
    return (
        "((valid_from IS NULL OR valid_from <= %s) AND (valid_to IS NULL OR valid_to > %s))",
        [as_of, as_of],
    )


def record_conflict_decision(
    *,
    tenant_id: str,
    classification: str,
    action: str,
    new_fact_id: int | None,
    existing_fact_id: int | None,
    reasoning: str = "",
    similarity: float | None = None,
    new_fact_text: str = "",
    existing_fact_text: str = "",
) -> int | None:
    """Persist one conflict-resolution judgement. Returns the row id.

    The LLM classification that drives supersession was previously persisted
    nowhere — it deactivated rows and vanished. Its error rate has therefore
    never been measured, which is how a classifier known to be biased toward
    "new" kept that bias indefinitely.

    Never raises: a failure to record an audit row must not fail the write it
    is auditing. It logs loudly instead.
    """
    if classification not in KNOWN_CLASSIFICATIONS:
        logger.warning("record_conflict_decision: unknown classification %r", classification)
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO memory_conflict_decisions "
                "(tenant_id, new_fact_id, existing_fact_id, classification, reasoning, "
                " similarity, action, new_fact_text, existing_fact_text) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    tenant_id or DEFAULT_TENANT,
                    new_fact_id,
                    existing_fact_id,
                    classification,
                    (reasoning or "")[:2000],
                    similarity,
                    action,
                    (new_fact_text or "")[:2000],
                    (existing_fact_text or "")[:2000],
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as exc:
        logger.warning("record_conflict_decision failed: %s", exc)
        return None


def supersede_with_validity(
    old_id: int,
    new_id: int,
    *,
    tenant_id: str = "",
    classification: str = "update",
) -> None:
    """Supersede ``old_id`` by ``new_id``, bounding the old fact in world-time.

    Deliberately keeps the existing ``is_active = FALSE`` behaviour so retrieval
    is unchanged and this cannot regress recall. The addition is ``valid_to``:
    the old fact stops being true *then*, rather than never having been true.

    An UPDATE and a CONTRADICTION are both bounded here, but they are recorded
    distinctly by the caller. They differ in what the bound MEANS — for an
    update the old fact was genuinely true until now; for a contradiction one of
    the two was always wrong and the bound is a guess about which. Acting on
    that difference (e.g. keeping both and flagging for review) needs the error
    rate the decisions table is now collecting, so it is not attempted yet.
    """
    tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        # One statement, so a crash cannot leave the old fact hidden but
        # unbounded — which would be invisible to both the normal and the
        # point-in-time read path.
        cur.execute(
            """
            UPDATE memory_facts
            SET is_active = FALSE,
                superseded_by = %s,
                valid_to = COALESCE(valid_to, NOW()),
                updated_at = NOW()
            WHERE id = %s AND tenant_id = %s
            """,
            (new_id, old_id, tenant),
        )
        # The successor is true from now. COALESCE so a re-run cannot walk
        # valid_from forward and silently shorten the fact's history.
        cur.execute(
            """
            UPDATE memory_facts
            SET valid_from = COALESCE(valid_from, NOW())
            WHERE id = %s AND tenant_id = %s
            """,
            (new_id, tenant),
        )
        conn.commit()
    logger.debug(
        "supersede_with_validity: %s -> %s (%s) for %s", old_id, new_id, classification, tenant
    )

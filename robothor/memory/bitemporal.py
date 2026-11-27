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
    from collections.abc import Mapping
    from datetime import datetime

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.memory.confirmed import NOT_OPERATOR_CONFIRMED_SQL

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
    force: bool = False,
    reason: str | None = None,
) -> bool:
    """Supersede ``old_id`` by ``new_id``, bounding the old fact in world-time.

    Returns whether ``old_id`` was superseded. An operator-confirmed fact is
    refused (a no-op, logged) unless ``force=True``: callers already skip such
    facts, and this is the defence in depth for one that does not.

    Deliberately keeps the existing ``is_active = FALSE`` behaviour so retrieval
    is unchanged and this cannot regress recall. The addition is ``valid_to``:
    the old fact stops being true *then*, rather than never having been true.

    An UPDATE and a CONTRADICTION are both bounded here, but they are recorded
    distinctly by the caller. They differ in what the bound MEANS — for an
    update the old fact was genuinely true until now; for a contradiction one of
    the two was always wrong and the bound is a guess about which. Acting on
    that difference (e.g. keeping both and flagging for review) needs the error
    rate the decisions table is now collecting, so it is not attempted yet.

    ``reason`` (``"changed"`` for a same-slot value change) is written to the
    old row's ``metadata.superseded_reason``, and the old id appended to the
    new row's ``metadata.supersedes``, in the same transaction.
    """
    tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        # One statement, so a crash cannot leave the old fact hidden but
        # unbounded — which would be invisible to both the normal and the
        # point-in-time read path.
        guard = "" if force else f"AND {NOT_OPERATOR_CONFIRMED_SQL}"
        cur.execute(
            f"""
            UPDATE memory_facts
            SET is_active = FALSE,
                superseded_by = %s,
                valid_to = COALESCE(valid_to, NOW()),
                updated_at = NOW()
            WHERE id = %s AND tenant_id = %s {guard}
            """,
            (new_id, old_id, tenant),
        )
        if not force and cur.rowcount == 0:
            logger.info(
                "supersede_with_validity: fact %s not superseded by %s "
                "(operator-confirmed or absent) for %s",
                old_id,
                new_id,
                tenant,
            )
            return False
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
        if reason:
            # Why it closed, on the old row; which rows it closed, on the new
            # one, so a read can show the earlier value next to the current.
            cur.execute(
                "UPDATE memory_facts SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('superseded_reason', %s::text) "
                "WHERE id = %s AND tenant_id = %s",
                (reason, old_id, tenant),
            )
            cur.execute(
                "UPDATE memory_facts SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
                "'{supersedes}', COALESCE(metadata->'supersedes', '[]'::jsonb) || to_jsonb(%s::int)) "
                "WHERE id = %s AND tenant_id = %s",
                (old_id, new_id, tenant),
            )
        conn.commit()
    logger.debug(
        "supersede_with_validity: %s -> %s (%s) for %s", old_id, new_id, classification, tenant
    )
    return True


def keep_overruled(new_id: int, kept_id: int, *, tenant_id: str = "") -> bool:
    """Record ``new_id`` as overruled by ``kept_id`` without touching ``kept_id``.

    For a new extraction that conflicts with an operator-confirmed fact. One
    statement on the new row only: inactive, ``superseded_by`` the kept fact,
    and a zero-width validity interval (it was never the believed state). The
    kept fact's own ``valid_from`` is left alone; writing it here would make the
    operator's fact "true only from now", and a point-in-time read before that
    moment would return the extraction it overruled.

    Returns whether the row was written. It never deactivates ``kept_id`` or
    any operator-confirmed row, even if the insert's dedup answered the kept
    fact's own id as ``new_id``; that case is a no-op.
    """
    tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE memory_facts
            SET is_active = FALSE,
                superseded_by = %s,
                valid_from = COALESCE(valid_from, NOW()),
                valid_to = COALESCE(valid_to, NOW()),
                updated_at = NOW()
            WHERE id = %s AND tenant_id = %s AND id <> %s
              AND {NOT_OPERATOR_CONFIRMED_SQL}
            """,
            (kept_id, new_id, tenant, kept_id),
        )
        written = bool(cur.rowcount > 0)
        conn.commit()
    if not written:
        logger.info("keep_overruled: fact %s left alone (kept %s) for %s", new_id, kept_id, tenant)
    return written


# ── Read time: the current value first, earlier ones labelled ───────────────

#: Earlier values shown after one current row, and in one recall overall.
EARLIER_PER_ROW = 1
MAX_EARLIER_ROWS = 3


def label_superseded(row: dict[str, Any]) -> dict[str, Any]:
    """Mark a closed row for the reader: ``superseded`` and when and why.

    ``superseded_at`` is the row's ``valid_to`` (ISO), ``superseded_reason``
    its ``metadata.superseded_reason`` (``"changed"`` for a same-slot value
    change, None for a row closed before reasons were written).
    ``superseded_by`` is the column, already on the row.
    """
    row["superseded"] = True
    valid_to = row.get("valid_to")
    iso = getattr(valid_to, "isoformat", None)
    row["superseded_at"] = iso() if callable(iso) else valid_to
    meta = row.get("metadata")
    row["superseded_reason"] = meta.get("superseded_reason") if isinstance(meta, dict) else None
    return row


def earlier_ids(row: Mapping[str, Any]) -> list[int]:
    """The ids a current row replaced (``metadata.supersedes``), newest first."""
    meta = row.get("metadata")
    raw = meta.get("supersedes") if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        return []
    ids = [int(i) for i in raw if isinstance(i, int) or (isinstance(i, str) and i.isdigit())]
    return sorted(set(ids), reverse=True)[:EARLIER_PER_ROW]


def place_superseded(
    rows: list[dict[str, Any]],
    *,
    earlier: Mapping[Any, list[dict[str, Any]]] | None = None,
    current: Mapping[Any, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Order recall rows so a closed value never outranks its current one.

    * A row with ``superseded_by`` set is labelled (:func:`label_superseded`)
      and placed directly after its current row: where that row stands when it
      is in ``rows``, else after ``current[old_id]`` (the visible current row,
      brought in at the old row's place), else where it was.
    * ``earlier[current_id]`` rows (the values a current row replaced) follow
      it, labelled, unless already placed.

    Rows without supersession pass through in order, untouched.
    """
    earlier = earlier or {}
    current = current or {}
    ids = {r.get("id") for r in rows if r.get("kind", "fact") == "fact"}
    deferred: dict[Any, list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []
    placed: set[Any] = set()

    def _emit(row: dict[str, Any]) -> None:
        if row.get("id") in placed:
            return
        placed.add(row.get("id"))
        out.append(row)
        for old in [*deferred.pop(row.get("id"), []), *earlier.get(row.get("id"), [])]:
            if old.get("id") in placed:
                continue
            label_superseded(old)
            rel = row.get("relevance")
            if rel is not None:
                old["relevance"] = min(float(old.get("relevance") or rel), float(rel))
            _emit(old)

    for row in rows:
        is_fact = row.get("kind", "fact") == "fact"
        by = row.get("superseded_by") if is_fact else None
        if by is None:
            _emit(row)
            continue
        label_superseded(row)
        if by in ids and by not in placed:
            deferred.setdefault(by, []).append(row)
            continue
        head = current.get(row.get("id"))
        if head is not None and head.get("id") not in placed:
            deferred.setdefault(head.get("id"), []).append(row)
            _emit(head)
            continue
        _emit(row)
    for rest in deferred.values():  # current row never emitted: keep the old ones
        for old in rest:
            _emit(old)
    return out

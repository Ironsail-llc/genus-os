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
    cur: Any = None,
) -> bool:
    """Supersede ``old_id`` by ``new_id``, bounding the old fact in world-time.

    Returns whether ``old_id`` was superseded. An operator-confirmed fact is
    refused (a no-op, logged) unless ``force=True``: callers already skip such
    facts, and this is the defence in depth for one that does not. A fact is
    never superseded by itself, and only an active fact is: closing a closed
    one again would rewrite its history.

    Deliberately keeps the existing ``is_active = FALSE`` behaviour so retrieval
    is unchanged and this cannot regress recall. The addition is ``valid_to``:
    the old fact stops being true *then*, rather than never having been true.

    An UPDATE and a CONTRADICTION are both bounded here, but they are recorded
    distinctly by the caller. They differ in what the bound MEANS — for an
    update the old fact was genuinely true until now; for a contradiction one of
    the two was always wrong and the bound is a guess about which.

    ``reason`` (``"changed"`` for a same-slot value change) is written to the
    old row's ``metadata.superseded_reason``, and the old id added once to the
    new row's ``metadata.supersedes``. ``cur``: run on the caller's cursor,
    inside its transaction (the insert of ``new_id``), without committing.
    """
    if int(old_id) == int(new_id):
        logger.warning("supersede_with_validity: fact %s cannot supersede itself", old_id)
        return False
    if cur is not None:
        return _supersede(cur, old_id, new_id, tenant_id, classification, force, reason)
    with get_connection() as conn:
        done = _supersede(conn.cursor(), old_id, new_id, tenant_id, classification, force, reason)
        if done:
            conn.commit()
    return done


def _supersede(
    cur: Any,
    old_id: int,
    new_id: int,
    tenant_id: str,
    classification: str,
    force: bool,
    reason: str | None,
) -> bool:
    tenant = tenant_id or DEFAULT_TENANT
    # One statement, so a crash cannot leave the old fact hidden but unbounded
    # — which would be invisible to both the normal and the point-in-time path.
    guard = "" if force else f"AND {NOT_OPERATOR_CONFIRMED_SQL}"
    cur.execute(
        f"""
        UPDATE memory_facts
        SET is_active = FALSE,
            superseded_by = %s,
            valid_to = COALESCE(valid_to, NOW()),
            updated_at = NOW()
        WHERE id = %s AND tenant_id = %s AND id <> %s AND is_active = TRUE {guard}
        """,
        (new_id, old_id, tenant, new_id),
    )
    if cur.rowcount == 0:
        logger.info(
            "supersede_with_validity: fact %s not superseded by %s "
            "(operator-confirmed, inactive or absent) for %s",
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
        # Why it closed, on the old row; which rows it closed, on the new one
        # (once each), so a read can show the earlier value next to the current.
        cur.execute(
            "UPDATE memory_facts SET metadata = COALESCE(metadata, '{}'::jsonb) "
            "|| jsonb_build_object('superseded_reason', %s::text) "
            "WHERE id = %s AND tenant_id = %s",
            (reason, old_id, tenant),
        )
        _add_supersedes(cur, new_id, old_id, tenant)
    logger.debug(
        "supersede_with_validity: %s -> %s (%s) for %s", old_id, new_id, classification, tenant
    )
    return True


def _add_supersedes(cur: Any, current_id: int, earlier_id: int, tenant: str) -> None:
    """Add ``earlier_id`` to ``current_id``'s ``metadata.supersedes``, once."""
    cur.execute(
        "UPDATE memory_facts SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
        "'{supersedes}', COALESCE(metadata->'supersedes', '[]'::jsonb) || to_jsonb(%s::int)) "
        "WHERE id = %s AND tenant_id = %s "
        "AND NOT COALESCE(metadata->'supersedes', '[]'::jsonb) @> to_jsonb(%s::int)",
        (earlier_id, current_id, tenant, earlier_id),
    )


def close_on_arrival(
    new_id: int,
    current_id: int,
    *,
    tenant_id: str = "",
    cur: Any = None,
) -> bool:
    """Store ``new_id`` as an earlier value of ``current_id``: it was true before.

    For a value change whose event is older than the current value's: it never
    replaces the current value. The new row is closed as it arrives
    (inactive, ``superseded_by`` the current fact, reason ``earlier``), valid
    from its own ``event_time`` until the current fact's, and the current row
    lists it in ``metadata.supersedes`` so recall shows it as the earlier value.
    The current fact is otherwise untouched. ``cur``: as in
    :func:`supersede_with_validity`.
    """
    if int(new_id) == int(current_id):
        return False
    tenant = tenant_id or DEFAULT_TENANT

    def _run(c: Any) -> bool:
        c.execute(
            """
            UPDATE memory_facts n
            SET is_active = FALSE,
                superseded_by = c.id,
                valid_from = COALESCE(n.event_time, n.valid_from, NOW()),
                valid_to = COALESCE(c.event_time, c.valid_from, NOW()),
                metadata = COALESCE(n.metadata, '{}'::jsonb)
                           || jsonb_build_object('superseded_reason', 'earlier'),
                updated_at = NOW()
            FROM memory_facts c
            WHERE n.id = %s AND n.tenant_id = %s AND c.id = %s AND c.tenant_id = %s
              AND n.id <> c.id AND n.is_active = TRUE
              AND COALESCE(n.metadata->'operator_confirmed', 'null'::jsonb) = 'null'::jsonb
            """,
            (new_id, tenant, current_id, tenant),
        )
        if c.rowcount == 0:
            return False
        _add_supersedes(c, current_id, new_id, tenant)
        return True

    if cur is not None:
        return _run(cur)
    with get_connection() as conn:
        done = _run(conn.cursor())
        conn.commit()
    return done


def keep_overruled(new_id: int, kept_id: int, *, tenant_id: str = "", cur: Any = None) -> bool:
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
    if cur is not None:
        return _overrule(cur, new_id, kept_id, tenant)
    with get_connection() as conn:
        written = _overrule(conn.cursor(), new_id, kept_id, tenant)
        conn.commit()
    return written


def _overrule(cur: Any, new_id: int, kept_id: int, tenant: str) -> bool:
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


#: Scores an earlier row has as zero, never None: callers format rows with
#: ``round(r.get("similarity", 0), 4)`` (see facts.py), and ``get`` returns a
#: present None. ``rerank_score`` stays None, its documented "not reranked".
_ZERO_SCORES = ("similarity", "rrf_score")


def _as_earlier(old: dict[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """``old`` labelled, shaped like ``current`` and placed under it.

    Every key a ranked row has is present: its scores zero (the row was not
    ranked, it follows its current row; ``rerank_score`` None), ``leg_scores``
    empty, and the current row's position markers (``leg_ranks``,
    ``fused_rank``, ``budget_tail``) so a re-sort keeps the two together.
    ``earlier_of`` names the current row.
    """
    label_superseded(old)
    for key in current:
        old.setdefault(key, None)
    for key in _ZERO_SCORES:
        old[key] = 0.0
    old["rerank_score"] = None
    old["fused_rank"] = current.get("fused_rank")
    old["leg_scores"] = {}
    old["leg_ranks"] = dict(current.get("leg_ranks") or {})
    if "budget_tail" in current:
        old["budget_tail"] = current["budget_tail"]
    rel = current.get("relevance")
    old["relevance"] = float(rel) if rel is not None else None
    old["earlier_of"] = current.get("id")
    return old


def _is_fact(row: Mapping[str, Any]) -> bool:
    return bool(row.get("kind", "fact") == "fact")


def place_superseded(
    rows: list[dict[str, Any]],
    *,
    earlier: Mapping[Any, list[dict[str, Any]]] | None = None,
    current: Mapping[Any, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Order recall rows so a closed value directly follows its current one.

    * ``earlier[current_id]``: the values a current row replaced, read for it.
      Each follows its current row (:func:`_as_earlier`).
    * A row in ``rows`` with ``superseded_by`` set (history recall): it moves
      directly after its current row when that row is in ``rows``; otherwise
      ``current[old_id]`` (the visible current row) takes the closed row's place
      and the closed row follows it; otherwise it stays where it was, labelled.
    * Extra rows (earlier values read for a current row, current rows brought
      in) are at most :data:`MAX_EARLIER_ROWS`; they never displace a row.
    * An earlier row that near-duplicates another row (not its own current row,
      which a reversal repeats word for word) is dropped, as ``_dedupe`` would.

    Rows without supersession keep their order and are untouched. Never
    mutates the mappings it is given while iterating them.
    """
    from robothor.memory.facts import _is_near_duplicate, _row_tokens

    earlier = earlier or {}
    current = current or {}
    ids = {r.get("id") for r in rows if _is_fact(r)}
    under: dict[Any, list[dict[str, Any]]] = {}
    ranked: list[dict[str, Any]] = []
    budget = MAX_EARLIER_ROWS
    for row in list(rows):
        by = row.get("superseded_by") if _is_fact(row) else None
        if by is None:
            ranked.append(row)
            continue
        label_superseded(row)
        if by in ids:
            under.setdefault(by, []).append(row)
            continue
        head = current.get(row.get("id"))
        if head is not None and head.get("id") not in ids and budget > 0:
            budget -= 1
            for key in ("leg_ranks", "budget_tail", "relevance"):
                if key in row and key not in head:
                    head[key] = row[key]
            ids.add(head.get("id"))
            ranked.append(head)
            under.setdefault(head.get("id"), []).append(row)
            continue
        ranked.append(row)

    ranked_tokens = {id(r): _row_tokens(r) for r in ranked}
    out: list[dict[str, Any]] = []
    placed: set[Any] = set()

    def _place(row: dict[str, Any]) -> None:
        placed.add(row.get("id") if _is_fact(row) else ("x", id(row)))
        out.append(row)

    for row in ranked:
        seen: Any = row.get("id") if _is_fact(row) else ("x", id(row))
        if seen in placed:
            continue
        _place(row)
        if not _is_fact(row):
            continue
        followers = list(under.get(row.get("id"), []))
        fetched = [dict(e) for e in earlier.get(row.get("id"), [])]
        for old in followers + fetched:
            if old.get("id") in placed:
                continue
            if old in fetched:
                if budget <= 0:
                    continue
                others = [t for r, t in ((r, ranked_tokens[id(r)]) for r in ranked) if r is not row]
                if _is_near_duplicate(_row_tokens(old), others):
                    continue
                budget -= 1
            _place(_as_earlier(old, row))
    # Rows whose current row never came (a cycle of closed rows): kept, labelled.
    for rest in list(under.values()):
        for old in rest:
            if old.get("id") not in placed:
                _place(old)
    return out

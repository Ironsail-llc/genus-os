"""Operator signals — real verdicts that anchor the goal-judge (Phase 2).

The judge *infers* operator satisfaction from words; these are the *ground truth*
that overrides the inference:

- **Reactions** — a 👍/👎/😡 the operator drops on a delivered message, mapped to
  a -2..+2 verdict and linked to the run that produced the message when we can
  resolve it.
- **Interventions** — the operator interrupting or steering a live run. Any
  intervention is a "you're doing it wrong" signal that clamps satisfaction down.

Pure mapping (``reaction_to_verdict``) is unit-testable; the recorders and the
judge-facing readers are thin DB wrappers that fail soft (a telemetry write must
never break a delivery or a run).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

# Emoji → verdict (-2..+2). Telegram reactions are a small fixed set; we map the
# common ones and treat the rest as neutral (0). Negative reactions dominate:
# one 😡 should outweigh several 👍 when the judge clamps.
_POSITIVE_STRONG = {"👍", "❤", "❤️", "🔥", "🎉", "👏", "🥰", "😍", "💯", "🙏"}
_POSITIVE_WEAK = {"👌", "😊", "🤝", "✅"}
_NEGATIVE_STRONG = {"👎", "💩", "🤬", "😡", "🤮", "😤"}
_NEGATIVE_WEAK = {"😕", "🙄", "😬", "😒"}


def reaction_to_verdict(emoji: str | None) -> int:
    """Map a reaction emoji to a -2..+2 operator verdict. Neutral (0) if unknown."""
    if not emoji:
        return 0
    e = emoji.strip()
    if e in _NEGATIVE_STRONG:
        return -2
    if e in _NEGATIVE_WEAK:
        return -1
    if e in _POSITIVE_STRONG:
        return 2
    if e in _POSITIVE_WEAK:
        return 1
    return 0


def record_reaction(
    *,
    chat_id: str,
    message_id: int,
    emoji: str | None,
    reactor: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Persist an operator reaction. Fails soft — never raises into the caller."""
    verdict = reaction_to_verdict(emoji)
    try:
        from robothor.crm.dal import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO message_reactions
                    (tenant_id, chat_id, message_id, agent_id, run_id, emoji, verdict, reactor)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    str(chat_id),
                    int(message_id),
                    agent_id,
                    run_id,
                    emoji,
                    verdict,
                    reactor,
                ),
            )
            conn.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("record_reaction failed (chat=%s msg=%s): %s", chat_id, message_id, exc)


def record_intervention(
    *,
    run_id: str | None,
    agent_id: str,
    kind: str,
    detail: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Persist an operator interrupt/steer. Fails soft."""
    if kind not in ("interrupt", "steer"):
        return
    try:
        from robothor.crm.dal import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO run_interventions (tenant_id, run_id, agent_id, kind, detail)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (tenant_id, run_id, agent_id, kind, (detail or "")[:500]),
            )
            conn.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("record_intervention failed (run=%s kind=%s): %s", run_id, kind, exc)


def resolve_reacted_message(
    message_id: int, tenant_id: str = DEFAULT_TENANT
) -> tuple[str | None, str | None]:
    """Best-effort (agent_id, run_id) for a reacted-to Telegram message.

    Outbound deliveries are persisted in chat_messages with the telegram message
    id and the producing run in the JSONB payload. Returns (None, None) when the
    message can't be resolved — the reaction is still recorded, just unlinked.
    """
    try:
        from robothor.crm.dal import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT message ->> 'author_agent_id', message ->> 'surfaced_from_run_id'
                FROM chat_messages
                WHERE message ->> 'telegram_message_id' = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(message_id),),
            )
            row = cur.fetchone()
            if row:
                return (row[0] or None), (row[1] or None)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("resolve_reacted_message failed for msg=%s: %s", message_id, exc)
    return None, None


def operator_verdict_for_run(
    cur: Any,
    run_id: str,
    agent_id: str,
    start: datetime,
    end: datetime,
    tenant_id: str = DEFAULT_TENANT,
) -> int | None:
    """The strongest real operator verdict bearing on a run, or None.

    Precedence, most negative wins (a single 😡 or interrupt dominates):
      1. A reaction linked directly to this run.
      2. Any intervention (interrupt/steer) on this run → -1.
      3. The worst reaction for this agent in the window (operator mood).
    Returns None when there is no real operator signal — the judge then falls
    back to pure inference.
    """
    # 1. Reaction on this exact run.
    cur.execute(
        "SELECT MIN(verdict) FROM message_reactions WHERE run_id = %s AND tenant_id = %s",
        (run_id, tenant_id),
    )
    row = cur.fetchone()
    run_reaction = row[0] if row else None

    # 2. Intervention on this run is a strong negative.
    cur.execute(
        "SELECT COUNT(*) FROM run_interventions WHERE run_id = %s AND tenant_id = %s",
        (run_id, tenant_id),
    )
    row = cur.fetchone()
    intervened = bool(row and row[0])

    candidates: list[int] = []
    if run_reaction is not None:
        candidates.append(int(run_reaction))
    if intervened:
        candidates.append(-1)

    if candidates:
        return min(candidates)

    # 3. Window-level operator mood for this agent (worst reaction).
    cur.execute(
        """
        SELECT MIN(verdict) FROM message_reactions
        WHERE agent_id = %s AND tenant_id = %s
          AND created_at >= %s AND created_at <= %s AND verdict <> 0
        """,
        (agent_id, tenant_id, start, end),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return None

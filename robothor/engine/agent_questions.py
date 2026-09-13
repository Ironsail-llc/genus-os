"""What an agent asked a person, as a row rather than as a coroutine's memory.

``permission_escalation.py`` holds a pending prompt in an ``asyncio.Event``
inside a process-local dict. That is exactly right for "the agent is mid-run and
the operator is at the keyboard": sub-minute, interactive, and denying on
timeout is safe because the operator is there to retry.

It is the wrong shape for the question the ``ask_user`` tool asks. The tool's
wait is bounded by the tool timeout — ten minutes at the outside — but the
*question* is not: an operator who sees it an hour later can still answer it
usefully, and the agent's next run can still read the answer. So the ask is
written down BEFORE the channel is called, and what comes back settles the row
rather than being the only place the answer ever existed.

The sibling table is ``workflow_approvals`` (``robothor/engine/approvals.py``),
and the two are deliberately not one table — ``crm/migrations/117`` states why
at length. Short version: the workflow resume driver acts on every decided row
it finds, and an answer is free text, not a verdict.

    from robothor.engine.agent_questions import ask_question, answer_question

    q = ask_question(run_id=..., agent_id="assistant", question="Which one?")
    ...
    answer_question(q.id, "the second", answered_by="operator")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

__all__ = [
    "AgentQuestion",
    "answer_question",
    "ask_question",
    "expire_overdue_questions",
    "get_question",
    "list_pending_questions",
]

#: Read-side cap for the list queries. A backlog longer than this is a
#: different problem than paging through it.
_LIST_LIMIT = 100

_COLUMNS = (
    "id, tenant_id, run_id, agent_id, kind, question, options, channel, target, "
    "status, answer, answered_by, answered_at, expires_at, created_at"
)


@dataclass
class AgentQuestion:
    """One question an agent asked, pending or settled."""

    id: str
    tenant_id: str
    run_id: str
    agent_id: str
    kind: str
    question: str
    status: str
    expires_at: datetime
    created_at: datetime
    options: list[str] = field(default_factory=list)
    channel: str = ""
    target: str = ""
    answer: str | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"


def _row(r: Any) -> AgentQuestion:
    options = r[6]
    if isinstance(options, str):  # a driver that hands back raw JSON text
        try:
            options = json.loads(options)
        except ValueError:
            options = []
    return AgentQuestion(
        id=str(r[0]),
        tenant_id=r[1],
        run_id=str(r[2]),
        agent_id=r[3],
        kind=r[4],
        question=r[5],
        options=list(options or []),
        channel=r[7],
        target=r[8],
        status=r[9],
        answer=r[10],
        answered_by=r[11],
        answered_at=r[12],
        expires_at=r[13],
        created_at=r[14],
    )


def ask_question(
    *,
    run_id: str,
    agent_id: str,
    question: str,
    options: list[str] | tuple[str, ...] = (),
    kind: str = "question",
    channel: str = "",
    target: str = "",
    timeout_seconds: float = 300.0,
    tenant_id: str = DEFAULT_TENANT,
) -> AgentQuestion:
    """Write the question down and return the row.

    Not idempotent, and that is the difference from
    ``approvals.request_approval``: a workflow step re-entered after a resume
    must find the SAME question, whereas a run that asks twice in one
    conversation has asked two questions.
    """
    from robothor.db.connection import get_connection

    expires_at = datetime.now(UTC) + timedelta(seconds=max(1.0, float(timeout_seconds)))
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO agent_questions
                   (tenant_id, run_id, agent_id, kind, question, options,
                    channel, target, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING {_COLUMNS}""",
            (
                tenant_id or DEFAULT_TENANT,
                run_id,
                agent_id,
                kind,
                question,
                json.dumps(list(options or [])),
                channel,
                target,
                expires_at,
            ),
        )
        row = cur.fetchone()
        conn.commit()

    asked = _row(row)
    logger.info(
        "Agent question recorded: id=%s kind=%s agent=%s channel=%s",
        asked.id,
        asked.kind,
        agent_id,
        channel or "none",
    )
    return asked


def get_question(question_id: str, *, tenant_id: str = DEFAULT_TENANT) -> AgentQuestion | None:
    """One question's current state, or None if this tenant never asked it."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_COLUMNS} FROM agent_questions WHERE id = %s AND tenant_id = %s",
            (question_id, tenant_id or DEFAULT_TENANT),
        )
        row = cur.fetchone()
    return _row(row) if row else None


def answer_question(
    question_id: str,
    answer: str,
    *,
    answered_by: str,
    tenant_id: str = DEFAULT_TENANT,
) -> bool:
    """Settle a question. True only if THIS call is what settled it.

    ``status <> 'answered'`` rather than ``status = 'pending'``: the first
    answer still wins, but a row the clock ran out on is deliberately still
    answerable. The tool stopped waiting; the operator did not stop caring, and
    a late answer is the only useful thing left about an expired question.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE agent_questions
                  SET status = 'answered', answer = %s, answered_by = %s, answered_at = NOW()
                WHERE id = %s AND tenant_id = %s AND status <> 'answered'""",
            (answer, answered_by, question_id, tenant_id or DEFAULT_TENANT),
        )
        settled = bool(cur.rowcount > 0)
        conn.commit()

    if settled:
        logger.info("Agent question answered: id=%s by=%s", question_id, answered_by)
    return settled


def expire_overdue_questions(*, tenant_id: str = DEFAULT_TENANT) -> list[AgentQuestion]:
    """Stamp past-deadline questions ``expired`` and return them.

    The row is KEPT. A DELETE here would destroy exactly the fact an operator
    needs when they later ask why an agent guessed instead of asking.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""UPDATE agent_questions
                   SET status = 'expired'
                 WHERE tenant_id = %s AND status = 'pending' AND expires_at <= NOW()
             RETURNING {_COLUMNS}""",
            (tenant_id or DEFAULT_TENANT,),
        )
        rows = cur.fetchall()
        conn.commit()

    expired = [_row(r) for r in rows]
    for q in expired:
        logger.warning(
            "Agent question expired unanswered: id=%s agent=%s channel=%s",
            q.id,
            q.agent_id,
            q.channel or "none",
        )
    return expired


def list_pending_questions(*, tenant_id: str = DEFAULT_TENANT) -> list[AgentQuestion]:
    """Open questions, nearest deadline first — what a person is holding up."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_COLUMNS} FROM agent_questions "
            "WHERE tenant_id = %s AND status = 'pending' "
            f"ORDER BY expires_at ASC LIMIT {_LIST_LIMIT}",
            (tenant_id or DEFAULT_TENANT,),
        )
        rows = cur.fetchall()
    return [_row(r) for r in rows]

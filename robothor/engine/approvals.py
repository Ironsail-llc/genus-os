"""Durable human-in-the-loop approvals for workflow steps.

The engine's existing approval path (``permission_escalation.py``) holds a
pending question in an ``asyncio.Event`` inside a process-local dict. That is
the right shape for "the agent is mid-run and the operator is at the
keyboard": sub-minute, interactive, and denying on timeout is safe because the
operator is there to retry.

It is the wrong shape for a workflow that asks "send this to the board?" and
waits overnight. The process restarts, the dict is empty, and the question is
gone with no row, no log, and no page — the run just reports a step failure
whose real cause is unrecoverable from stored data.

Here the question is a ROW. It survives restarts, it can be answered from any
channel (CLI, tool, dashboard), and every outcome — including "nobody
answered in time" — is written down rather than inferred from an absence.

    from robothor.engine.approvals import request_approval, decide_approval

    req = request_approval(run_id=..., step_id="confirm-send", prompt="...")
    ...
    decide_approval(run_id, "confirm-send", ApprovalDecision.APPROVED,
                    decided_by="operator")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

# Read-side cap for the driver queries. A tick that finds more decided
# approvals than this resumes the rest on the next tick rather than holding a
# cursor open across an unbounded resume loop.
_DRIVER_BATCH = 100


class ApprovalDecision(StrEnum):
    """Terminal states. ``PENDING`` is deliberately absent — it is what a row
    starts as, never something anyone decides."""

    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class ApprovalRequest:
    """One pending or settled question about one workflow step."""

    id: str
    tenant_id: str
    run_id: str
    workflow_id: str
    step_id: str
    prompt: str
    detail: str
    status: str
    expires_at: datetime
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    # True only for the caller whose INSERT actually created the row. Not
    # persisted — it is a fact about THIS call, and it is what stops a
    # resumed run from paging the operator a second time about a question
    # they are already looking at.
    newly_created: bool = False

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def approved(self) -> bool:
        return self.status == "approved"


_COLUMNS = (
    "id, tenant_id, run_id, workflow_id, step_id, prompt, detail, status, "
    "expires_at, created_at, decided_by, decided_at, decision_note"
)


def _row(r: Any) -> ApprovalRequest:
    return ApprovalRequest(
        id=str(r[0]),
        tenant_id=r[1],
        run_id=str(r[2]),
        workflow_id=r[3],
        step_id=r[4],
        prompt=r[5],
        detail=r[6],
        status=r[7],
        expires_at=r[8],
        created_at=r[9],
        decided_by=r[10],
        decided_at=r[11],
        decision_note=r[12],
    )


def request_approval(
    *,
    run_id: str,
    workflow_id: str,
    step_id: str,
    prompt: str,
    detail: str = "",
    timeout_hours: int = 24,
    tenant_id: str = DEFAULT_TENANT,
) -> ApprovalRequest:
    """Ask, or return the question already asked for this (run, step).

    Idempotent by construction. A resumed run re-enters the same step and
    must find the SAME question — including one that has already been
    answered — because re-asking would page the operator once per restart and
    would let a crash loop reopen a decision they already made.
    """
    from robothor.db.connection import get_connection

    expires_at = datetime.now(UTC) + timedelta(hours=max(1, timeout_hours))
    with get_connection() as conn:
        cur = conn.cursor()
        # ON CONFLICT DO NOTHING rather than DO UPDATE: the stored question,
        # its deadline, and any decision against it all outrank whatever this
        # caller is proposing. The SELECT below then returns what stands.
        cur.execute(
            """INSERT INTO workflow_approvals
                   (tenant_id, run_id, workflow_id, step_id, prompt, detail, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, step_id) DO NOTHING""",
            (
                tenant_id or DEFAULT_TENANT,
                run_id,
                workflow_id,
                step_id,
                prompt,
                detail,
                expires_at,
            ),
        )
        created = bool(cur.rowcount > 0)
        conn.commit()

        cur.execute(
            f"SELECT {_COLUMNS} FROM workflow_approvals WHERE run_id = %s AND step_id = %s",
            (run_id, step_id),
        )
        row = cur.fetchone()

    if row is None:  # pragma: no cover — the INSERT above guarantees a row
        raise RuntimeError(f"approval row vanished for {workflow_id}:{step_id}")

    req = _row(row)
    req.newly_created = created
    if created:
        logger.info(
            "Approval requested: workflow=%s step=%s run=%s expires=%s",
            workflow_id,
            step_id,
            run_id,
            expires_at.isoformat(),
        )
    return req


def get_approval(
    run_id: str, step_id: str, *, tenant_id: str = DEFAULT_TENANT
) -> ApprovalRequest | None:
    """The current state of one question, or None if it was never asked."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_COLUMNS} FROM workflow_approvals "
            "WHERE run_id = %s AND step_id = %s AND tenant_id = %s",
            (run_id, step_id, tenant_id or DEFAULT_TENANT),
        )
        row = cur.fetchone()
    return _row(row) if row else None


def decide_approval(
    run_id: str,
    step_id: str,
    decision: ApprovalDecision,
    *,
    decided_by: str,
    note: str = "",
    tenant_id: str = DEFAULT_TENANT,
) -> bool:
    """Settle a pending question. True if THIS call is what settled it.

    The ``status = 'pending'`` predicate makes the first decision win: a
    second operator, a double-tap on a Telegram button, or a retried tool
    call all return False and change nothing. Returning False is not an
    error — it means "already decided", which callers should report rather
    than treat as a failure.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE workflow_approvals
                  SET status = %s, decided_by = %s, decided_at = NOW(), decision_note = %s
                WHERE run_id = %s AND step_id = %s AND tenant_id = %s AND status = 'pending'""",
            (
                decision.value,
                decided_by,
                note,
                run_id,
                step_id,
                tenant_id or DEFAULT_TENANT,
            ),
        )
        settled = bool(cur.rowcount > 0)
        conn.commit()

    if settled:
        logger.info(
            "Approval %s: run=%s step=%s by=%s",
            decision.value,
            run_id,
            step_id,
            decided_by,
        )
    return settled


def expire_overdue_approvals(*, tenant_id: str = DEFAULT_TENANT) -> list[ApprovalRequest]:
    """Stamp past-deadline questions ``expired`` and return them.

    The row is KEPT. "Nobody answered" is the fact an operator needs when
    they later ask why a workflow aborted, and it is exactly the fact a
    cleanup DELETE would destroy. The caller applies the step's declared
    ``on_timeout`` policy to each returned row; this function only records
    that the clock ran out.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""UPDATE workflow_approvals
                   SET status = 'expired', decided_at = NOW(),
                       decided_by = COALESCE(decided_by, 'timeout')
                 WHERE tenant_id = %s AND status = 'pending' AND expires_at <= NOW()
             RETURNING {_COLUMNS}""",
            (tenant_id or DEFAULT_TENANT,),
        )
        rows = cur.fetchall()
        conn.commit()

    expired = [_row(r) for r in rows]
    for req in expired:
        logger.warning(
            "Approval expired unanswered: workflow=%s step=%s run=%s (asked %s)",
            req.workflow_id,
            req.step_id,
            req.run_id,
            req.created_at.isoformat() if req.created_at else "?",
        )
    return expired


def list_pending_approvals(*, tenant_id: str = DEFAULT_TENANT) -> list[ApprovalRequest]:
    """Open questions, oldest deadline first — what the operator is holding up."""
    return _list("status = 'pending'", "expires_at ASC", tenant_id)


def list_decided_approvals(*, tenant_id: str = DEFAULT_TENANT) -> list[ApprovalRequest]:
    """Answered questions whose runs may now be resumable.

    Includes ``expired`` so the resume driver applies the timeout policy
    through the same path as a human decision — one resume mechanism, not
    two.
    """
    return _list("status IN ('approved', 'rejected', 'expired')", "decided_at ASC", tenant_id)


def _list(where: str, order: str, tenant_id: str) -> list[ApprovalRequest]:
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_COLUMNS} FROM workflow_approvals "
            f"WHERE tenant_id = %s AND {where} ORDER BY {order} LIMIT {_DRIVER_BATCH}",
            (tenant_id or DEFAULT_TENANT,),
        )
        rows = cur.fetchall()
    return [_row(r) for r in rows]

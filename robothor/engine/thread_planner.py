"""Forward thread planner — Stage 4 of the thread pool work.

Takes a stalled thread, looks at its objective + recent history + body,
decides what to do next: spawn a worker to advance it, ask the operator a
specific question, wait, or close. Replaces the stage-3 stall2 bare-flag
flip: instead of just setting requires_human=true when a thread goes stale,
the planner runs first and only escalates when it *actually* has no next
move — and when it does, it surfaces a concrete question, not a bare flag.

Heuristic-only in v1 — no LLM calls. Reads crm_task_history + body text.
Gated by ROBOTHOR_PLANNER_ENABLED=1; off → no-op, stage-3 behavior
unchanged.

Distinct from robothor.engine.planner (per-run LLM planning for a single
agent execution).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from robothor.constants import DEFAULT_TENANT
from robothor.engine.autonomy import classify_action, load_tenant_defaults
from robothor.engine.thread_pool import Thread

logger = logging.getLogger(__name__)

PLANNER_VERSION = 1
PLAN_STALE_SECONDS = 4 * 3600  # re-plan if last plan is older than 4h
CHASE_AFTER_HOURS = 48  # email sent this long ago → chase

Action = Literal["execute", "ask", "wait", "close"]


@dataclass(frozen=True)
class PlanResult:
    """The planner's verdict for a single thread."""

    task_id: str
    action: Action
    next_action: str | None
    next_action_agent: str | None
    question_for_operator: str | None
    rationale: str


# ─── Body parsing helpers ──────────────────────────────────────────────


_THREAD_ID_RE = re.compile(r"threadId:\s*([a-zA-Z0-9_-]+)")
_FROM_RE = re.compile(r"(?m)^from:\s*([^\s<]+@[^\s>]+)")
_OBJECTIVE_RE = re.compile(r"(?im)^objective:\s*(.+)$")
_MISSING_DATUM_KEYWORDS = [
    "pricing",
    "price",
    "quote",
    "spec",
    "specs",
    "document",
    "contract",
    "term",
    "terms",
    "api",
    "rate",
]


def _extract_missing_datum(objective: str, body: str) -> str:
    """Pick a phrase from the objective that names the missing thing.
    Defaults to the first noun-ish keyword we recognize, or the literal
    objective if nothing matches."""
    low = objective.lower()
    for kw in _MISSING_DATUM_KEYWORDS:
        if kw in low:
            return kw
    # fall back to first word after "Confirm/Get/Obtain"
    m = re.search(r"(?i)\b(confirm|get|obtain|collect)\s+(\w+)", objective)
    if m:
        return m.group(2)
    return "the requested information"


def _latest_history_kind(history: list[dict[str, Any]]) -> tuple[str | None, timedelta | None]:
    """Return (kind, age) of the most recent history row that carries a
    metadata.kind. Pure status transitions (task creation, manual moves) have
    no kind and are skipped — the planner only cares about semantic events
    like email_sent, calendar_offer_received, plan, ask."""
    if not history:
        return None, None

    def _ts(row: dict[str, Any]) -> datetime:
        ts = row.get("created_at")
        if isinstance(ts, datetime):
            return ts
        return datetime.min.replace(tzinfo=UTC)

    kinded = []
    for row in history:
        meta = row.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("kind"):
            kinded.append(row)
    if not kinded:
        return None, None
    newest = max(kinded, key=_ts)
    meta = newest.get("metadata") or {}
    kind = meta.get("kind") if isinstance(meta, dict) else None
    ts = _ts(newest)
    now = datetime.now(UTC)
    age = now - ts if ts > datetime.min.replace(tzinfo=UTC) else None
    return kind, age


def _count_recent_offers(history: list[dict[str, Any]], kind: str, within: timedelta) -> int:
    now = datetime.now(UTC)
    cnt = 0
    for row in history:
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        if meta.get("kind") != kind:
            continue
        ts = row.get("created_at")
        if not isinstance(ts, datetime):
            continue
        if now - ts <= within:
            cnt += 1
    return cnt


# ─── Core planner ─────────────────────────────────────────────────────


def plan_thread(
    thread: Thread,
    body: str,
    history: list[dict[str, Any]],
    autonomy: dict[str, Any],
    objective: str,
    question_for_operator: str | None = None,
    next_action: str | None = None,
    last_planned_at: datetime | None = None,
) -> PlanResult:
    """Decide the next action for one thread. Pure function, no DB writes."""

    # Skip — already waiting on the operator.
    if question_for_operator:
        return PlanResult(
            task_id=thread.id,
            action="wait",
            next_action=None,
            next_action_agent=None,
            question_for_operator=None,
            rationale="pending_operator_answer",
        )

    # Skip — plan is still fresh.
    if (
        next_action
        and last_planned_at
        and (datetime.now(UTC) - last_planned_at).total_seconds() < PLAN_STALE_SECONDS
    ):
        return PlanResult(
            task_id=thread.id,
            action="wait",
            next_action=None,
            next_action_agent=None,
            question_for_operator=None,
            rationale="fresh_plan_exists",
        )

    latest_kind, latest_age = _latest_history_kind(history)

    # Pattern: vendor keeps sending booking links while objective forbids meetings
    booking_offers = _count_recent_offers(
        history, "calendar_offer_received", within=timedelta(days=7)
    )
    if booking_offers >= 2 and "without" in objective.lower() and "meeting" in objective.lower():
        return PlanResult(
            task_id=thread.id,
            action="ask",
            next_action=None,
            next_action_agent=None,
            question_for_operator=(
                f"{thread.title}: vendor has responded with booking links {booking_offers} "
                "time(s) and hasn't answered the emailed questions. Drop this vendor "
                "and focus on the alternative? y/n"
            ),
            rationale="objective_veto_meeting_repeated",
        )

    # Pattern: we emailed and silence for 48h+ — chase
    if (
        latest_kind == "email_sent"
        and latest_age is not None
        and latest_age >= timedelta(hours=CHASE_AFTER_HOURS)
    ):
        datum = _extract_missing_datum(objective, body)
        action_type = "vendor_data_ask"
        verdict = classify_action(
            action_type,
            metadata={
                "reversible": True,
                "estimated_cost_usd": 0,
                "objective": objective,
            },
            budget=autonomy,
        )
        if verdict == "auto":
            return PlanResult(
                task_id=thread.id,
                action="execute",
                next_action=(
                    f"Chase {_sender_name(body)} for {datum} via email — "
                    "reference prior ask, request short written answer, no meeting."
                ),
                next_action_agent="email-responder",
                question_for_operator=None,
                rationale=f"email_sent_{int(latest_age.total_seconds() / 3600)}h_ago_no_reply",
            )
        if verdict == "refuse":
            return PlanResult(
                task_id=thread.id,
                action="ask",
                next_action=None,
                next_action_agent=None,
                question_for_operator=(
                    f"{thread.title}: autonomy budget refused chase — what next?"
                ),
                rationale="autonomy_refuse",
            )

    # No recognized pattern — surface a concrete question asking what to do.
    return PlanResult(
        task_id=thread.id,
        action="ask",
        next_action=None,
        next_action_agent=None,
        question_for_operator=(
            f"{thread.title}: no automatic next step recognized (age {thread.age_days}d, "
            f"stale {thread.stale_days}d). Should I drop, continue, or hand off?"
        ),
        rationale="no_pattern_matched",
    )


def _sender_name(body: str) -> str:
    m = _FROM_RE.search(body or "")
    if not m:
        return "the vendor"
    addr = m.group(1)
    # Return the part before @
    return addr.split("@", 1)[0].replace(".", " ").title()


# ─── Apply (DB writes) ────────────────────────────────────────────────


def apply_plan(
    plan: PlanResult,
    tenant_id: str = DEFAULT_TENANT,
    dry_run: bool = False,
) -> bool:
    """Persist a PlanResult. Executes set_next_action or set_question depending
    on plan.action. `wait` and `close` are no-ops here (close is handled by
    auto_close_completed_threads; wait means "nothing changed").

    ``dry_run=True`` skips the DB writes entirely — callers get the PlanResult
    with no side effects. Always safe to use from smoke tests or debugging.
    """
    if dry_run:
        logger.debug(
            "apply_plan dry_run: %s action=%s next=%s question=%s",
            plan.task_id,
            plan.action,
            plan.next_action,
            plan.question_for_operator,
        )
        return True
    # Lazy import so tests can patch robothor.crm.dal.*
    from robothor.crm import dal

    if plan.action == "execute":
        if not plan.next_action:
            logger.warning("apply_plan execute without next_action for %s", plan.task_id)
            return False
        return bool(
            dal.set_next_action(
                task_id=plan.task_id,
                next_action=plan.next_action,
                agent=plan.next_action_agent,
                by="planner",
                planner_version=PLANNER_VERSION,
                tenant_id=tenant_id,
            )
        )
    if plan.action == "ask":
        if not plan.question_for_operator:
            logger.warning("apply_plan ask without question for %s", plan.task_id)
            return False
        return bool(
            dal.set_question(
                task_id=plan.task_id,
                question=plan.question_for_operator,
                by="planner",
                tenant_id=tenant_id,
            )
        )
    return False


# ─── Driver for heartbeat warmup ─────────────────────────────────────


_CANDIDATE_SQL = """
SELECT
    t.id::text AS id,
    t.title,
    t.status,
    COALESCE(t.priority, 'normal') AS priority,
    t.body,
    t.objective,
    t.next_action,
    t.next_action_agent,
    t.question_for_operator,
    t.autonomy_budget,
    t.last_planned_at,
    t.requires_human,
    COALESCE(t.escalation_count, 0) AS escalation_count,
    t.assigned_to_agent,
    GREATEST(0, EXTRACT(DAY FROM (NOW() - t.created_at))::int) AS age_days,
    GREATEST(0, EXTRACT(DAY FROM (NOW() - COALESCE(t.updated_at, t.created_at)))::int) AS stale_days,
    (t.sla_deadline_at IS NOT NULL AND t.sla_deadline_at < NOW()) AS sla_breached,
    (
        SELECT COUNT(*) FROM crm_tasks c
        WHERE c.parent_task_id = t.id AND c.deleted_at IS NULL
    ) AS total_children,
    (
        SELECT COUNT(*) FROM crm_tasks c
        WHERE c.parent_task_id = t.id
          AND c.deleted_at IS NULL AND c.status != 'DONE'
    ) AS open_children
FROM crm_tasks t
WHERE t.deleted_at IS NULL
  AND t.tenant_id = %s
  AND t.status NOT IN ('DONE', 'CANCELED')
  AND 'thread' = ANY(t.tags)
  AND (
      t.last_planned_at IS NULL
      OR t.last_planned_at < NOW() - INTERVAL '4 hours'
  )
ORDER BY COALESCE(t.last_planned_at, t.created_at) ASC
LIMIT %s
"""


def _load_planner_candidates(tenant_id: str, max_threads: int) -> list[dict[str, Any]]:
    """Fetch threads that need re-planning. Safe to fail → returns []."""
    from robothor.db.connection import get_connection

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SET LOCAL statement_timeout = '3s'")
            cur.execute(_CANDIDATE_SQL, (tenant_id, max_threads))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
    except Exception as e:
        logger.debug("Planner candidate query failed: %s", e)
        return []


def _load_history(task_id: str, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
    from robothor.db.connection import get_connection

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT metadata, created_at
                   FROM crm_task_history
                   WHERE task_id = %s AND tenant_id = %s
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (task_id, tenant_id, limit),
            )
            return [{"metadata": m, "created_at": ts} for (m, ts) in cur.fetchall()]
    except Exception as e:
        logger.debug("Planner history query failed for %s: %s", task_id, e)
        return []


def _row_to_thread(row: dict[str, Any]) -> Thread:
    return Thread(
        id=row["id"],
        title=row["title"] or "",
        status=row["status"] or "TODO",
        priority=row["priority"] or "normal",
        age_days=int(row["age_days"] or 0),
        stale_days=int(row["stale_days"] or 0),
        requires_human=bool(row.get("requires_human")),
        sla_breached=bool(row.get("sla_breached")),
        escalation_count=int(row.get("escalation_count") or 0),
        open_children=int(row.get("open_children") or 0),
        total_children=int(row.get("total_children") or 0),
        assigned_to_agent=row.get("assigned_to_agent"),
    )


def plan_all_stalled(
    tenant_id: str = DEFAULT_TENANT,
    max_threads: int = 8,
    dry_run: bool = False,
) -> list[PlanResult]:
    """Run the planner across all stalled threads and apply the results.

    Gated by ROBOTHOR_PLANNER_ENABLED=1 — off (default) → no-op, returns [].
    Set ``dry_run=True`` to see what the planner would do without writing
    anything to the DB. Never raises; swallows errors so the warmup hook
    stays safe.
    """
    if not dry_run and os.environ.get("ROBOTHOR_PLANNER_ENABLED") != "1":
        return []

    try:
        candidates = _load_planner_candidates(tenant_id, max_threads)
    except Exception as e:
        logger.debug("plan_all_stalled load failed: %s", e)
        return []

    defaults = load_tenant_defaults(tenant_id)
    plans: list[PlanResult] = []
    for row in candidates:
        try:
            thread = _row_to_thread(row)
            history = _load_history(thread.id, tenant_id)
            objective = row.get("objective") or row.get("title") or ""
            autonomy = row.get("autonomy_budget") or {}
            if isinstance(autonomy, dict) and autonomy:
                merged = dict(defaults)
                merged.update(autonomy)
                autonomy = merged
            else:
                autonomy = defaults
            plan = plan_thread(
                thread=thread,
                body=row.get("body") or "",
                history=history,
                autonomy=autonomy,
                objective=objective,
                question_for_operator=row.get("question_for_operator"),
                next_action=row.get("next_action"),
                last_planned_at=row.get("last_planned_at"),
            )
            apply_plan(plan, tenant_id=tenant_id, dry_run=dry_run)
            plans.append(plan)
        except Exception as e:
            logger.debug("plan_thread failed for %s: %s", row.get("id"), e)
    return plans

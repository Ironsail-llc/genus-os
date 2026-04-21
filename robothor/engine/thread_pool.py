"""Thread pool — ordered view of open multi-beat work for the main heartbeat.

A "thread" is a ``crm_tasks`` row tagged ``thread`` that represents ongoing work
spanning multiple heartbeat cycles — research questions, stuck experiments,
broken agents, overdue reports, PR follow-through. This module reads the pool;
creation is inline via ``update_task(tags=[..., 'thread'])`` from heartbeat
instructions.

Priority order (first → last):
1. Operator-blocked: ``requires_human`` AND status = REVIEW
2. SLA-breached: ``sla_deadline_at < now``
3. Most-escalated: ``escalation_count`` desc
4. Oldest-touched: ``COALESCE(updated_at, created_at)`` asc

Injected into Main's warmup via ``register_agent_context_hook``. Never raises —
a failing hook silently returns ``None``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

MAX_THREADS = 8
MAX_TITLE_CHARS = 60
MAX_LINE_CHARS = 140

# Stall classifier thresholds (Stage 3).
# stall1: log a note but stay silent; stall2: flip to REVIEW + requires_human;
# stall3: surface to the operator via Phase 3 "Need You" — the collaboration ping.
STALL1_DAYS = 1
STALL2_DAYS = 2
STALL3_DAYS = 3

# Acceptance blocks: fenced ```accept … ``` inside a task body.
# Each non-blank, non-comment line is an independent shell command run
# via subprocess. ALL must exit 0 for acceptance to pass.
_ACCEPT_BLOCK_RE = re.compile(
    r"```accept\s*\n(?P<body>.*?)\n```",
    re.DOTALL,
)
ACCEPT_TIMEOUT_SECONDS = 30

# Pending-sub-agent marker — inserted into a thread body by Main when it
# spawns a sub-agent to advance the thread. Filters out the thread from
# list_threads() so the next heartbeat doesn't spawn a duplicate.
# Format: <!-- pending: run=<run_id> ts=<iso8601> -->
_PENDING_MARKER_RE = re.compile(
    r"<!--\s*pending:\s*run=(?P<run_id>[\w\-]+)\s+ts=(?P<ts>[\w:\.\-\+T]+)\s*-->"
)
PENDING_EXPIRY_SECONDS = 1800  # match sub_agent_timeout_seconds default


@dataclass(frozen=True)
class Thread:
    """One thread-tagged open task with derived status signals."""

    id: str
    title: str
    status: str
    priority: str
    age_days: int
    stale_days: int
    requires_human: bool
    sla_breached: bool
    escalation_count: int
    open_children: int
    total_children: int
    assigned_to_agent: str | None

    @property
    def short_id(self) -> str:
        return self.id[:8]


_LIST_SQL = """
SELECT
  t.id::text,
  t.title,
  t.status,
  COALESCE(t.priority, 'normal'),
  t.requires_human,
  COALESCE(t.escalation_count, 0),
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
    WHERE c.parent_task_id = t.id AND c.deleted_at IS NULL AND c.status != 'DONE'
  ) AS open_children,
  t.body
FROM crm_tasks t
WHERE t.deleted_at IS NULL
  AND t.tenant_id = %s
  AND t.status != 'DONE'
  AND 'thread' = ANY(t.tags)
  AND (t.follow_up_at IS NULL OR t.follow_up_at <= NOW())
ORDER BY
  CASE WHEN t.requires_human AND t.status = 'REVIEW' THEN 0 ELSE 1 END,
  CASE WHEN t.sla_deadline_at IS NOT NULL AND t.sla_deadline_at < NOW() THEN 0 ELSE 1 END,
  COALESCE(t.escalation_count, 0) DESC,
  COALESCE(t.updated_at, t.created_at) ASC
LIMIT %s
"""


def list_threads(
    tenant_id: str = DEFAULT_TENANT,
    limit: int = MAX_THREADS,
    include_pending: bool = False,
) -> list[Thread]:
    """Return open thread-tagged tasks ordered by staleness priority.

    By default, threads with an unexpired pending-sub-agent marker in their
    body are filtered out so the same thread isn't spawned twice in adjacent
    heartbeats. Pass ``include_pending=True`` to see them (useful for the
    stall classifier in Stage 3).

    Fast enough (<50ms on typical fleet) to run in a 100ms warmup hook budget.
    """
    from robothor.db.connection import get_connection

    # Fetch more than `limit` so post-filter of pending threads can still
    # surface `limit` non-pending ones.
    fetch_limit = limit * 2 if not include_pending else limit
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '3s'")
        cur.execute(_LIST_SQL, (tenant_id, fetch_limit))
        rows = cur.fetchall()

    threads: list[Thread] = []
    for r in rows:
        body = r[12]
        if not include_pending and is_pending(body):
            continue
        threads.append(
            Thread(
                id=r[0],
                title=r[1] or "(untitled)",
                status=r[2],
                priority=r[3],
                requires_human=bool(r[4]),
                escalation_count=int(r[5]),
                assigned_to_agent=r[6],
                age_days=int(r[7]),
                stale_days=int(r[8]),
                sla_breached=bool(r[9]),
                total_children=int(r[10]),
                open_children=int(r[11]),
            )
        )
        if len(threads) >= limit:
            break
    return threads


def format_thread_pool(threads: list[Thread]) -> str:
    """Format threads as a compact text block for warmup injection.

    Always returns a non-empty string — an empty pool produces a prompt to
    promote ongoing work; a non-empty pool produces one line per thread.
    """
    if not threads:
        return (
            "--- THREAD POOL ---\n"
            "(empty) No open threads tagged `thread`. If you notice multi-heartbeat "
            "work slipping through the cracks (research, stuck experiments, overdue "
            "reports, stale PRs), promote it: update_task(id, tags=[..., 'thread'])."
        )

    lines = ["--- THREAD POOL — advance, close, escalate, or propose at least one per beat ---"]
    for t in threads:
        title = t.title
        if len(title) > MAX_TITLE_CHARS:
            title = title[: MAX_TITLE_CHARS - 1] + "…"

        markers: list[str] = []
        stall = classify_stall(t)
        if stall != "fresh":
            markers.append(f"[{stall}]")
        if t.requires_human and t.status == "REVIEW":
            markers.append("🧑OPERATOR")
        if t.sla_breached:
            markers.append("⏰SLA")
        if t.escalation_count > 0:
            markers.append(f"↑{t.escalation_count}")
        if t.total_children:
            done = t.total_children - t.open_children
            markers.append(f"kids:{done}/{t.total_children}")

        marker_str = " ".join(markers)
        assignee = f" @{t.assigned_to_agent}" if t.assigned_to_agent else ""
        # Emit the full 36-char UUID so the model can pass it directly to
        # get_task / update_task. The 8-char short_id prefix caused failed
        # tool calls that had to be recovered via search_records.
        line = (
            f"[{t.id}][{t.status}][{t.stale_days}d] {title}"
            + (f"  {marker_str}" if marker_str else "")
            + assignee
        )
        if len(line) > MAX_LINE_CHARS:
            line = line[: MAX_LINE_CHARS - 1] + "…"
        lines.append(line)
    return "\n".join(lines)


def _thread_pool_context(config: AgentConfig) -> str | None:
    """Agent context hook — inject thread pool view for main agent heartbeat.

    Runs the auto-sweep (Stage 3) before reading so Main sees any newly
    completed parent threads in the REVIEW bucket. Returns None for any agent
    other than main so other agents' warmups aren't cluttered, and None for
    interactive runs (Telegram, webchat, channel wake) so a direct operator
    ask isn't buried under thread-management directives. Swallows all
    exceptions to match the warmup contract.
    """
    if config.id != "main":
        return None
    # Only inject into scheduled heartbeat runs, not interactive chat turns.
    from robothor.engine.warmup import current_warmup_kind

    if current_warmup_kind() != "cron":
        return None
    try:
        tenant_id = os.environ.get("ROBOTHOR_TENANT_ID", "") or DEFAULT_TENANT
        try:
            swept = auto_close_completed_threads(tenant_id=tenant_id)
        except Exception as exc:
            logger.debug("Auto-sweep failed: %s", exc)
            swept = []
        planner_count = 0
        try:
            from robothor.engine.thread_planner import plan_all_stalled

            plans = plan_all_stalled(tenant_id=tenant_id)
            planner_count = sum(1 for p in plans if p.action in ("execute", "ask"))
        except Exception as exc:
            logger.debug("Thread planner failed: %s", exc)
        threads = list_threads(tenant_id=tenant_id)
        formatted = format_thread_pool(threads)
        if swept:
            formatted += f"\n(auto-sweep: flipped {len(swept)} parent thread(s) to REVIEW)"
        if planner_count:
            formatted += f"\n(planner: decided next-step for {planner_count} thread(s))"
        return formatted
    except Exception as exc:
        logger.debug("Thread pool hook failed: %s", exc)
        return None


# ─── Acceptance blocks (Stage 2) ──────────────────────────────────────


def parse_accept_block(body: str | None) -> list[str]:
    """Extract shell commands from a ``` ```accept … ``` ``` fenced block in a
    task body.

    Non-blank, non-comment (``#``-prefixed) lines become individual commands.
    Returns an empty list if no accept block is present.
    """
    if not body:
        return []
    m = _ACCEPT_BLOCK_RE.search(body)
    if not m:
        return []
    return [
        line.strip()
        for line in m.group("body").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def run_accept(
    commands: list[str],
    cwd: str | os.PathLike[str] | None = None,
    timeout: int = ACCEPT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Execute an acceptance block.

    Each command runs in its own shell. All must exit 0 for ``passed: True``.
    Returns a dict suitable for recording in ``crm_task_history.metadata``.
    """
    if not commands:
        return {"passed": True, "failures": [], "ran": 0}

    failures: list[dict[str, object]] = []
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            failures.append(
                {"command": cmd, "exit_code": None, "error": f"timeout after {timeout}s"}
            )
            continue
        if result.returncode != 0:
            failures.append(
                {
                    "command": cmd,
                    "exit_code": result.returncode,
                    "stdout": (result.stdout or "")[-400:],
                    "stderr": (result.stderr or "")[-400:],
                }
            )
    return {"passed": not failures, "failures": failures, "ran": len(commands)}


# ─── Pending sub-agent marker (Stage 2) ───────────────────────────────


def is_pending(body: str | None, now_iso: str | None = None) -> bool:
    """Return True if the task body has a non-expired pending marker.

    A pending thread is in flight — some prior heartbeat already spawned
    a sub-agent for it. The marker expires after PENDING_EXPIRY_SECONDS so
    a truly wedged sub-agent doesn't block the thread forever.
    """
    if not body:
        return False
    m = _PENDING_MARKER_RE.search(body)
    if not m:
        return False
    try:
        from datetime import UTC, datetime

        ts = datetime.fromisoformat(m.group("ts"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return (now - ts).total_seconds() < PENDING_EXPIRY_SECONDS
    except (ValueError, TypeError):
        return False


def pending_marker(run_id: str, ts_iso: str | None = None) -> str:
    """Render a pending marker for insertion into a task body."""
    from datetime import UTC, datetime

    ts = ts_iso or datetime.now(UTC).isoformat(timespec="seconds")
    return f"<!-- pending: run={run_id} ts={ts} -->"


# ─── Stall classifier (Stage 3) ───────────────────────────────────────


def classify_stall(thread: Thread) -> str:
    """Return a stall tier for a thread: fresh | stall1 | stall2 | stall3.

    - ``fresh``: <= STALL1_DAYS days since update
    - ``stall1``: >= STALL1_DAYS days — log a note, no escalation
    - ``stall2``: >= STALL2_DAYS or escalation_count >= 1 — flip to REVIEW
    - ``stall3``: >= STALL3_DAYS in REVIEW+requires_human — ping the operator
    """
    if thread.requires_human and thread.status == "REVIEW" and thread.stale_days >= STALL3_DAYS:
        return "stall3"
    if thread.stale_days >= STALL2_DAYS or thread.escalation_count >= 1:
        return "stall2"
    if thread.stale_days >= STALL1_DAYS:
        return "stall1"
    return "fresh"


# ─── Auto-sweep (Stage 3) ─────────────────────────────────────────────


def auto_close_completed_threads(tenant_id: str = DEFAULT_TENANT) -> list[str]:
    """Find thread-tagged parent tasks where all children are DONE but the
    parent isn't, and flip them to REVIEW with a note.

    Main picks them up in its next heartbeat's REVIEW scan and decides to
    close or re-open. Returns the list of task IDs that were flipped.

    Fast, idempotent, no side effects beyond the status flip. Runs inside
    the warmup executor, so the statement is hard-bounded to 3s to prevent
    a row-lock contention from stalling the heartbeat.
    """
    from robothor.db.connection import get_connection

    sql = """
    SELECT t.id::text
    FROM crm_tasks t
    WHERE t.deleted_at IS NULL
      AND t.tenant_id = %s
      AND t.status NOT IN ('DONE', 'REVIEW')
      AND 'thread' = ANY(t.tags)
      AND EXISTS (
        SELECT 1 FROM crm_tasks c
        WHERE c.parent_task_id = t.id AND c.deleted_at IS NULL
      )
      AND NOT EXISTS (
        SELECT 1 FROM crm_tasks c
        WHERE c.parent_task_id = t.id
          AND c.deleted_at IS NULL
          AND c.status != 'DONE'
      )
    """
    flipped: list[str] = []
    with get_connection() as conn:
        cur = conn.cursor()
        # Bound every statement in this txn so a stale row lock can't hang
        # the warmup. 3s is well above typical (<50ms) but short enough that
        # a stall never leaks into the stall-watchdog's 300s budget.
        cur.execute("SET LOCAL statement_timeout = '3s'")
        cur.execute(sql, (tenant_id,))
        candidate_ids = [r[0] for r in cur.fetchall()]
        for task_id in candidate_ids:
            cur.execute(
                """UPDATE crm_tasks
                   SET status = 'REVIEW',
                       updated_at = NOW()
                   WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s
                """,
                (task_id, tenant_id),
            )
            if cur.rowcount:
                flipped.append(task_id)
        conn.commit()
    return flipped

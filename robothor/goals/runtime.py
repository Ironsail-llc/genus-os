"""Task-local goal binding; child agents cannot mutate their coordinator's goal."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig, AgentRun, SpawnContext
    from robothor.engine.session import AgentSession
    from robothor.engine.tools.dispatch import ToolContext


@dataclass
class Binding:
    tenant: str
    goal_id: str
    attempt: str
    token_remaining: int | None = None
    run_id: str = ""
    yield_requested: bool = False
    runs: dict[str, AgentRun] = field(default_factory=dict)


binding: ContextVar[Binding | None] = ContextVar("pursuit_goal", default=None)


def attach_run(run: AgentRun) -> None:
    current = binding.get()
    if current:
        current.runs[run.id] = run
    if current and run.trigger_detail == f"goal:{current.goal_id}" and not current.run_id:
        current.run_id = run.id
        from robothor.goals.store import transaction

        with transaction() as cur:
            cur.execute(
                """UPDATE pursuit_goal_attempts SET run_id=%s
                           WHERE tenant_id=%s AND id=%s AND status='running'""",
                (run.id, current.tenant, current.attempt),
            )


def apply_budget(run: AgentRun) -> None:
    current = binding.get()
    if current and run.id == current.run_id and current.token_remaining is not None:
        run.token_budget = (
            min(run.token_budget, current.token_remaining)
            if run.token_budget
            else current.token_remaining
        )


def budget_hit() -> bool:
    current = binding.get()
    return bool(
        current
        and current.token_remaining is not None
        and sum(r.input_tokens + r.output_tokens for r in current.runs.values())
        >= current.token_remaining
    )


def pursuit_yielded(run: AgentRun | None) -> bool:
    current = binding.get()
    return bool(current and run and current.run_id == run.id and current.yield_requested)


def stop_pursuit(session: AgentSession) -> bool:
    return pursuit_yielded(session.run) or stop_at_budget(session)


def stop_at_budget(session: AgentSession) -> bool:
    if not budget_hit():
        return False
    session.run.budget_exhausted = True
    session.record_error("Explicit goal token budget exhausted")
    return True


def admit_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> None:
    # Deferred invocation must obey the underlying tool's read/write contract.
    # The normal dispatcher still enforces its allow-list on the nested call.
    if name == "tool_call":
        nested = args.get("name")
        arguments = args.get("arguments", {})
        if not isinstance(nested, str) or nested == "tool_call" or not isinstance(arguments, dict):
            raise ValueError("invalid deferred goal tool invocation")
        name, args = nested, arguments
    current = binding.get()
    if current is None:
        from robothor.engine.session_registry import lookup

        session = lookup(ctx.run_id) if ctx.run_id else None
        task_id = getattr(session.run, "task_id", None) if session else None
        task_ids = {task_id, args.get("parent_task_id")}
        if any(task and not task_runnable(task, ctx.tenant_id) for task in task_ids):
            raise ValueError("the goal owning this task is inactive")
        return
    from robothor.engine.tools.constants import READONLY_TOOLS
    from robothor.goals.store import transaction

    with transaction() as cur:
        cur.execute(
            """SELECT g.status,g.data,g.lease_id,s.enabled FROM pursuit_goals g
                       LEFT JOIN goal_pursuit_settings s ON s.tenant_id=g.tenant_id
                       WHERE g.tenant_id=%s AND g.id=%s""",
            (current.tenant, current.goal_id),
        )
        row = cur.fetchone()
    if not row or str(row["lease_id"]) != current.attempt or not row["enabled"]:
        raise ValueError("goal execution is no longer authorized")
    if row["status"] in {"paused", "canceled", "blocked"} or budget_hit():
        raise ValueError("goal execution has stopped")
    if row["status"] in {"waiting", "complete", "review"} and name not in READONLY_TOOLS:
        raise ValueError("goal has finished this work period; end the turn")
    if (
        row["data"]["recovery_required"]
        and name not in READONLY_TOOLS
        and not (name == "update_pursuit_goal" and args.get("action") == "reconciled")
    ):
        raise ValueError(
            "inspect previous run results, then record reconciled before taking more actions"
        )


def prepare_task(
    cur: Any, tenant: str, title: str, body: str | None, assigned: str | None
) -> str | None:
    """Deduplicate task creation across retries while holding the caller's transaction."""
    current = binding.get()
    if not current or current.tenant != tenant:
        return None
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + tenant,))
    cur.execute(
        """SELECT t.id FROM pursuit_goal_tasks l JOIN crm_tasks t ON t.id=l.task_id AND t.tenant_id=l.tenant_id
                   WHERE l.tenant_id=%s AND l.goal_id=%s AND t.title=%s
                   AND COALESCE(t.body,'')=%s AND t.assigned_to_agent IS NOT DISTINCT FROM %s
                   AND t.deleted_at IS NULL LIMIT 1""",
        (tenant, current.goal_id, title, body or "", assigned),
    )
    row = cur.fetchone()
    return str(row["id"]) if row else None


def link_created_task(cur: Any, tenant: str, task_id: str) -> None:
    current = binding.get()
    if current and current.tenant == tenant:
        cur.execute(
            """INSERT INTO pursuit_goal_tasks(tenant_id,goal_id,task_id) VALUES (%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
            (tenant, current.goal_id, task_id),
        )


def task_runnable(task_id: str, tenant: str) -> bool:
    from robothor.goals.store import transaction

    with transaction() as cur:
        cur.execute("SELECT pursuit_task_runnable(%s,%s) AS runnable", (task_id, tenant))
        return bool(cur.fetchone()["runnable"])


def initialize_token_budget(
    run: AgentRun, agent_config: AgentConfig, spawn_context: SpawnContext | None = None
) -> None:
    """Resolve tracking, inherited and explicitly requested pursuit budgets once."""
    from robothor.engine.model_registry import compute_token_budget

    auto_budget = compute_token_budget(agent_config.model_primary, agent_config.max_iterations)
    run.token_budget = auto_budget
    if spawn_context and spawn_context.remaining_token_budget > 0:
        run.token_budget = (
            min(auto_budget, spawn_context.remaining_token_budget)
            if auto_budget > 0
            else spawn_context.remaining_token_budget
        )
    apply_budget(run)

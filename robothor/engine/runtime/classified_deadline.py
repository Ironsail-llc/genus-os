"""Tighten the native timeout when its existing planner identifies simple work."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from robothor.db.connection import get_connection

if TYPE_CHECKING:
    from collections.abc import Iterator

    from robothor.engine.models import AgentRun
    from robothor.engine.runtime.contracts import ExecutionContext, RunRequest

_admission: ContextVar[tuple[RunRequest, datetime] | None] = ContextVar(
    "classified_action_admission", default=None
)


def admitted_request() -> RunRequest | None:
    scope = _admission.get()
    return scope[0] if scope else None


@contextmanager
def admission(request: RunRequest, admitted_at: datetime | None = None) -> Iterator[None]:
    token = _admission.set((request, admitted_at or datetime.now(UTC)))
    try:
        yield
    finally:
        _admission.reset(token)


def eligible(request: RunRequest, config: Any) -> bool:
    options, context = request.options, request.context
    from robothor.engine.run_context import in_benchmark_run

    return not (
        request.resume_from
        or context.goal_id
        or context.parent_id
        or options.get("trigger_type") not in {"webchat", "telegram"}
        or options.get("spawn_context")
        or options.get("readonly_mode")
        or options.get("deep_plan")
        or getattr(config, "is_benchmark", False)
        or in_benchmark_run()
    )


def _deadline(config: Any, route: Any, plan: Any) -> datetime | None:
    scope = _admission.get()
    if scope is None:
        return None
    request, started = scope
    context = request.context
    if not eligible(request, config):
        return None
    difficulty = getattr(config, "difficulty_class", "")
    if not difficulty:
        difficulty = plan.difficulty if plan and plan.success else getattr(route, "difficulty", "")
    if difficulty != "simple":
        return None
    from robothor.engine.runtime.action_policy import SIMPLE_ACTION_SECONDS

    deadline = started + timedelta(seconds=SIMPLE_ACTION_SECONDS)
    return min(deadline, context.deadline) if context.deadline else deadline


def _persist(run: AgentRun, context: ExecutionContext) -> None:
    if context.deadline is None:
        # Every caller replaces the context with a deadline first; saying so
        # here beats an AttributeError inside the UPDATE's parameter tuple.
        raise RuntimeError("Unable to persist classified action deadline: none set")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runs SET runtime_context=jsonb_set(runtime_context,
               '{deadline}',to_jsonb(%s::text)) WHERE id=%s AND tenant_id=%s
               AND runtime_context->>'request_id'=%s""",
            (context.deadline.isoformat(), run.id, context.tenant_id, context.request_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Unable to persist classified action deadline")


async def apply(session: Any, window: asyncio.Timeout, config: Any, route: Any, plan: Any) -> None:
    from robothor.engine.runtime import classification_window
    from robothor.engine.runtime.current import active_context
    from robothor.engine.runtime.deadlines import require_time

    deadline = _deadline(config, route, plan)
    unclassified = deadline is None
    context = active_context.get()
    identified_long = (
        plan
        and plan.success
        and plan.difficulty in ("moderate", "complex")
        and isinstance(getattr(plan, "raw", None), dict)
        and plan.raw.get("difficulty") == plan.difficulty
    )
    if deadline is None:
        if identified_long:
            classification_window.release()
            return
        deadline = classification_window.owned_deadline()
    if deadline is None or context is None:
        return
    if context.deadline is not None and context.deadline <= deadline:
        if not unclassified:
            classification_window.release()
        return
    context = replace(context, deadline=deadline)
    active_context.set(context)  # Restored by the enclosing runtime admission.
    if unclassified:
        # Preserve the original classification owner and its interruption contract.
        require_time(context)
        await asyncio.to_thread(_persist, session.run, context)
        return
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    when = asyncio.get_running_loop().time() + max(0, remaining)
    # Bound once: `when()` was called twice, so the None check did not narrow
    # the value actually passed to min().
    current_when = window.when()
    window.reschedule(min(current_when, when) if current_when is not None else when)
    require_time(context)
    classification_window.release()
    await asyncio.to_thread(_persist, session.run, context)

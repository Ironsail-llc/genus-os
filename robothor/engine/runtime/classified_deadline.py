"""Tighten the native timeout when its existing planner identifies simple work."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from robothor.db.connection import get_connection

_admission = ContextVar("classified_action_admission", default=None)


@contextmanager
def admission(request, admitted_at=None):
    token = _admission.set((request, admitted_at or datetime.now(UTC)))
    try:
        yield
    finally:
        _admission.reset(token)


def eligible(request, config):
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


def _deadline(config, route, plan):
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


def _persist(run, context):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE agent_runs SET runtime_context=jsonb_set(runtime_context,
               '{deadline}',to_jsonb(%s::text)) WHERE id=%s AND tenant_id=%s
               AND runtime_context->>'request_id'=%s""",
            (context.deadline.isoformat(), run.id, context.tenant_id, context.request_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Unable to persist classified action deadline")


async def apply(session, window, config, route, plan):
    from robothor.engine.runtime import classification_window
    from robothor.engine.runtime.current import active_context
    from robothor.engine.runtime.deadlines import require_time

    deadline = _deadline(config, route, plan)
    context = active_context.get()
    if deadline is None or context is None:
        if (
            plan
            and plan.success
            and plan.difficulty in ("moderate", "complex")
            and isinstance(getattr(plan, "raw", None), dict)
            and getattr(plan, "raw", {}).get("difficulty") == plan.difficulty
        ):
            classification_window.release()
        return
    if context.deadline is not None and context.deadline <= deadline:
        classification_window.release()
        return
    context = replace(context, deadline=deadline)
    active_context.set(context)  # Restored by the enclosing runtime admission.
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    when = asyncio.get_running_loop().time() + max(0, remaining)
    window.reschedule(min(window.when(), when) if window.when() is not None else when)
    require_time(context)
    classification_window.release()
    await asyncio.to_thread(_persist, session.run, context)

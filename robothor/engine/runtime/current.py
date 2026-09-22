"""Thin current-engine adapter; no second retry, compaction or delivery loop."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from robothor.engine.runtime.contracts import (
    ExecutionContext,
    ProgressEvent,
    RunRequest,
    RuntimeResult,
    StateEnvelope,
    Usage,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from datetime import datetime

    from robothor.engine.models import AgentRun

active_context: ContextVar[ExecutionContext | None] = ContextVar("runtime_context", default=None)


class CurrentRuntime:
    identity = StateEnvelope()

    def __init__(
        self,
        execute: Callable[..., Awaitable[AgentRun]],
        *,
        audit_admission: bool = False,
        acceptance_attempted: bool = False,
        admitted_at: datetime | None = None,
    ) -> None:
        self._execute = execute
        self._audit_admission = audit_admission
        self._acceptance_attempted = acceptance_attempted
        self._admitted_at = admitted_at

    async def run(
        self,
        request: RunRequest,
        on_event: Callable[[ProgressEvent], Awaitable[None]] | None = None,
    ) -> RuntimeResult:
        from robothor.engine.runtime.action_policy import apply_action_deadline
        from robothor.engine.runtime.deadlines import constrain_context, execute_before_deadline

        request = apply_action_deadline(request)
        # Admission reads and progress delivery consume the same deadline as
        # execution; a stalled checkpoint must not defer the start of the clock.
        entered = [False]
        try:
            from robothor.engine.runtime.classification_window import guard
            from robothor.engine.runtime.classified_deadline import admission

            with admission(request, self._admitted_at):
                async with guard(request, self._admitted_at):
                    return await execute_before_deadline(
                        constrain_context(request.context),
                        lambda: self._run(request, on_event, entered),
                    )
        except (TimeoutError, asyncio.CancelledError) as exc:
            from robothor.engine.runtime.classification_window import ClassificationDeadlineError
            from robothor.engine.runtime.deadlines import remaining

            if self._audit_admission and isinstance(exc, ClassificationDeadlineError):
                # Aliased: `admission_audit` exports a `record_timeout` too, with a
                # different signature, and the two were bound to one name in this
                # single function body. It worked only because each branch calls its
                # own immediately; a reordering would have picked the wrong one.
                from robothor.engine.runtime.classification_audit import (
                    record_timeout as record_classification_timeout,
                )

                await record_classification_timeout(request, exc.deadline)
                raise
            left = remaining(request.context)
            if self._audit_admission and not entered[0] and left is not None and left <= 0:
                from robothor.engine.runtime.admission_audit import (
                    record_timeout as record_admission_timeout,
                )

                await record_admission_timeout(request)
            raise

    async def _run(
        self,
        request: RunRequest,
        on_event: Callable[[ProgressEvent], Awaitable[None]] | None,
        # A one-element mutable box, not a clock: `run` reads `entered[0]`
        # in its own except handler to tell a timeout before execution
        # started from one during it.
        entered: list[bool],
    ) -> RuntimeResult:
        from robothor.engine.models import StepType
        from robothor.engine.runtime.deadlines import constrain_context

        context = constrain_context(request.context)
        from robothor.goals.runtime import binding

        goal = binding.get()
        if context.goal_id and (
            not goal
            or (goal.tenant, goal.goal_id, goal.attempt)
            != (context.tenant_id, context.goal_id, context.attempt_id)
        ):
            raise ValueError("goal runtime requires the active trusted lease binding")
        options = dict(request.options)
        if "tenant_id" in options and options["tenant_id"] != context.tenant_id:
            raise ValueError("runtime tenant mismatch")
        if request.resume_from:
            if request.checkpoint is None:
                raise ValueError("resumption requires a compatible checkpoint envelope")
            self.identity.require_compatible(request.checkpoint)
            from robothor.engine.checkpoint import CheckpointManager

            saved = await asyncio.to_thread(
                CheckpointManager.load_latest, request.resume_from, tenant_id=context.tenant_id
            )
            if saved is None:
                raise ValueError("checkpoint unavailable; refusing to repeat the original request")
            from robothor.engine.runtime.controls import stopped

            if await asyncio.to_thread(stopped, context.tenant_id, request.resume_from):
                raise ValueError("stopped runs cannot be resumed implicitly")
            from robothor.engine.runtime.resume_deadline import restore

            context = restore(context, saved)
            options["resume_from_run_id"] = request.resume_from
        elif "resume_from_run_id" in options:
            raise ValueError("use the explicit resume contract")
        started = time.monotonic()
        callback = options.get("on_status")

        async def status(event: dict[str, Any]) -> None:
            if on_event:
                await on_event(
                    ProgressEvent(
                        context.request_id,
                        event.get("event", "progress"),
                        time.monotonic() - started,
                        event,
                    )
                )
            if callback:
                await callback(event)

        if (callback or on_event) and not self._acceptance_attempted:
            from robothor.engine.runtime.acceptance import emit

            await emit(status, context)
        options.update(tenant_id=context.tenant_id, correlation_id=context.request_id)
        if callback or on_event:
            options["on_status"] = status
        from robothor.engine.runtime.activity import Activity, current, remove

        running = asyncio.current_task()
        if running is None:  # pragma: no cover - _run only ever runs inside a task
            raise RuntimeError("runtime activity requires a running task")
        activity = Activity(
            running,
            asyncio.get_running_loop(),
            goal_id=context.goal_id,
            parent_goal_id=context.parent_goal_id,
        )
        activity_token = current.set(activity)
        token = active_context.set(context)
        from robothor.engine.runtime.deadlines import require_time

        try:
            require_time(context)
            entered[0] = True
            from robothor.engine.runtime.deadlines import execute_before_deadline

            run = await execute_before_deadline(
                context,
                lambda: self._execute(
                    agent_id=request.agent_id, message=request.message, **options
                ),
            )
        finally:
            remove(activity)
            current.reset(activity_token)
            active_context.reset(token)
        return RuntimeResult(
            run=run,
            usage=Usage(
                sum(s.step_type == StepType.LLM_CALL for s in run.steps),
                run.input_tokens,
                run.output_tokens,
                run.total_cost_usd,
            ),
            verified=run.verified_status == "verified",
            unresolved=str(run.status) in {"cancelled", "timeout", "failed"},
        )

    async def control(
        self, tenant: str, run_id: str, action: str, note: str = ""
    ) -> dict[str, Any]:
        from robothor.engine.runtime.controls import issue

        return await asyncio.to_thread(issue, tenant, run_id, action, note)


def run_identity(run: Any) -> dict[str, Any]:
    """Persist actual resolved attribution, with optional goal binding, on every run."""
    from robothor.goals.runtime import binding

    context = active_context.get()
    goal = binding.get()
    return {
        **asdict(StateEnvelope()),
        "tenant_id": run.tenant_id,
        "principal_id": run.user_id or "service:" + run.agent_id,
        "request_id": context.request_id if context else (run.correlation_id or run.id),
        "parent_id": run.parent_run_id,
        "resume_from_run_id": getattr(run, "resume_from_run_id", None),
        "goal_id": goal.goal_id if goal else None,
        "parent_goal_id": context.parent_goal_id if context else None,
        "attempt_id": goal.attempt if goal else None,
        "budget_id": context.budget_id if context else (goal.attempt if goal else None),
        "deadline": context.deadline.isoformat() if context and context.deadline else None,
    }


def runtime_entrypoint(
    execute: Callable[..., Awaitable[AgentRun]],
) -> Callable[..., Coroutine[Any, Any, AgentRun]]:
    """Keep the public runner signature while routing every admission through the adapter."""
    from functools import wraps
    from inspect import signature
    from uuid import uuid4

    parameters = signature(execute)

    @wraps(execute)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> AgentRun:
        from datetime import UTC, datetime

        from robothor.engine.runtime.profile_admission import prepare, resolved_profile

        admitted_at = datetime.now(UTC)
        values = dict(parameters.bind(self, *args, **kwargs).arguments)
        values.pop("self")
        agent_id, message = values.pop("agent_id"), values.pop("message")
        from robothor.db.connection import current_tenant_scope

        inherited = active_context.get()
        tenant = (
            values.get("tenant_id")
            or current_tenant_scope()
            or (inherited.tenant_id if inherited else None)
            or self.config.tenant_id
        )
        if not tenant or (inherited and inherited.tenant_id != tenant):
            raise ValueError("runtime tenant identity missing or inconsistent")
        values["tenant_id"] = tenant
        spawn = values.get("spawn_context")
        context = ExecutionContext(
            tenant,
            values.get("user_id") or "service:" + agent_id,
            values.get("correlation_id") or (inherited.request_id if inherited else str(uuid4())),
            parent_id=getattr(spawn, "parent_run_id", None)
            or (inherited.parent_id if inherited else None),
            goal_id=inherited.goal_id if inherited else None,
            attempt_id=inherited.attempt_id if inherited else None,
            budget_id=inherited.budget_id if inherited else None,
            deadline=inherited.deadline if inherited else None,
            parent_goal_id=inherited.parent_goal_id if inherited else None,
        )
        resume = values.pop("resume_from_run_id", None)
        request = RunRequest(
            context, agent_id, message, values, resume, StateEnvelope() if resume else None
        )

        from robothor.engine.runtime.acceptance import before_lookup

        acceptance_attempted = await before_lookup(request)
        request, resolution = await prepare(self, request, admitted_at)

        async def native(**options: Any) -> AgentRun:
            from robothor.engine.runtime.setup import watchdog_scope

            with resolved_profile(resolution), watchdog_scope():
                return await execute(self, **options)

        runtime = CurrentRuntime(
            native,
            audit_admission=True,
            acceptance_attempted=acceptance_attempted,
            admitted_at=admitted_at,
        )
        return (await runtime.run(request)).run

    return wrapped

"""One model turn's tool calls: admitted in order, run in batches, recorded in order.

Extracted from ``_run_loop``, which the module ratchet caps at its current
size, and extracted along a real seam rather than to make room: everything in
here has exactly one subject — the calls that arrived on a single assistant
message — and nothing in here reads the loop's iteration state except to name
it in a lifecycle event.

The turn runs in three phases, and the phases are the design:

1. **Admit, sequentially, in the model's order.** Every gate in
   ``tool_admission.py`` — plan mode, ``tools_allowed``, the PRE_TOOL_USE hook,
   the guardrail engine (including human approval) and the system-run RBAC
   check — runs exactly as it did when the loop executed one call at a time.
   Concurrency is a property of the EXECUTION phase only; no gate ever runs
   beside another gate, so nothing about admission's order is weakened.
2. **Execute the admitted calls of one batch concurrently**, bounded by
   ``ROBOTHOR_PARALLEL_TOOL_CALLS``. What may share a batch is decided by
   ``parallel_tools.plan_batches`` and is narrow: classified read-only calls
   only, and the first call that is not one runs alone and forces the rest of
   the turn sequential in the model's order.
3. **Record, sequentially, in the model's order.** Post-execution guardrails,
   the POST_TOOL_USE hook, cost propagation, the step row, the outcome
   classification, escalation and the checkpoint all happen on the main task,
   one call at a time, in the order the model asked for them. That is what
   makes "results come back in the model's order with the same
   ``tool_call_id``s" a property of the structure instead of a hope, and it is
   why a refusal is not recorded the moment it is decided — a refusal at
   position 2 must still land after the result of position 1.

MEASURED 2026-09-16: ten WildClawBench runs issued **zero** parallel tool
calls while the competing harness batched on twenty turns of one task. Under a
fixed per-task budget, a hundred and fifty sequential calls is the difference
between finishing and running out of clock.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.engine.parallel_tools import parallel_limit, plan_batches
from robothor.engine.post_execution import apply_post_execution_guardrails
from robothor.engine.repeat_guard import drain_repeat_notes
from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.session import ENGINE_CONTEXT_ROLE
from robothor.engine.tool_outcome import record_tool_outcome
from robothor.engine.tool_turn_context import tool_turn_context
from robothor.engine.tools.read_only import declared_read_only_tools

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig
    from robothor.engine.session import AgentSession
    from robothor.engine.tool_admission import ToolVerdict

logger = logging.getLogger(__name__)

__all__ = [
    "ToolTurnMixin",
    "ToolTurnRequest",
    "execute_code_approval_cap",
    "execute_code_call_cap",
]


def execute_code_approval_cap() -> int:
    """How many human-approval prompts one snippet may raise. Never raises.

    Separate from the call cap and far smaller, because the two bound different
    things: the call cap bounds the engine's work, this one bounds a person's
    attention. 0 means a snippet may raise none at all.
    """
    try:
        from robothor.settings import get_settings

        return max(0, int(get_settings().engine.execute_code_max_approvals))
    except Exception:  # noqa: BLE001 - a missing config is the default, not a crash
        return 1


def execute_code_call_cap() -> int:
    """How many tools one snippet may call. Never raises.

    Read here rather than in the handler so the PROXY carries the cap: a cap
    the tool reads for itself is a cap the tool can be asked to ignore.
    """
    from robothor.engine.code_execution import DEFAULT_MAX_TOOL_CALLS

    try:
        from robothor.settings import get_settings

        configured = int(get_settings().engine.execute_code_max_calls)
    except Exception:  # noqa: BLE001 - a missing config is the default, not a crash
        return DEFAULT_MAX_TOOL_CALLS
    return max(1, configured)


@dataclass
class ToolTurnRequest:
    """Everything one turn needs, named once instead of eighteen arguments.

    A dataclass rather than a long signature because the call site is inside
    the loop this was extracted from: a fifteen-parameter call there is the
    same god-object in a different shape.
    """

    assistant_msg: Any
    session: AgentSession
    agent_config: AgentConfig
    iteration: int
    guardrail_engine: Any = None
    hook_registry: Any = None
    scratchpad: Any = None
    escalation: Any = None
    checkpoint: Any = None
    trace: Any = None
    on_tool: Any = None
    on_status: Any = None
    readonly_mode: bool = False
    readonly_tool_set: frozenset[str] = frozenset()
    allowed_tool_set: frozenset[str] = frozenset()
    guard_state: Any = None
    tool_failures: dict[str, int] = field(default_factory=dict)


@dataclass
class _Call:
    """One tool call's state as it moves through the three phases."""

    index: int
    tc: Any
    tool_name: str
    tool_args: dict[str, Any]
    verdict: ToolVerdict | None = None
    result: dict[str, Any] | None = None
    elapsed_ms: int = 0
    refused: bool = False


class ToolTurnMixin:
    """The tool-call half of ``AgentRunner``'s loop body."""

    if TYPE_CHECKING:
        # Mirrors of what the host class provides. Attributes rather than
        # method stubs: `AgentRunner` inherits both this mixin and
        # `ToolAdmissionMixin`, and a re-declared signature here would be a
        # SECOND definition of the same name across two bases — which mypy
        # rejects, correctly, as two answers to one question.
        registry: Any
        config: Any
        _active_watchdog: Any
        _admit_tool_call: Any
        _record_refusal: Any

    async def run_tool_turn(self, req: ToolTurnRequest) -> list[tuple[str, str, Any]]:
        """Admit, run and record every tool call on this assistant message.

        Returns the turn's ``(tool_name, error_message, error_type)`` list,
        which drives error recovery and the feedback injection above.
        """
        iteration_errors: list[tuple[str, str, Any]] = []
        calls = list(req.assistant_msg.tool_calls or [])
        names = [tc.function.name for tc in calls]
        from robothor.engine.goal_report_delivery import record_report_turn

        await self._emit_tools_start(req, names)

        plan = plan_batches(
            names,
            read_only=declared_read_only_tools(),
            human_approval=tuple(getattr(req.agent_config, "human_approval_tools", ()) or ()),
            limit=parallel_limit(),
        )
        # One id for the whole turn, and only when something actually ran
        # beside something else: a batch id on every single-call turn would
        # make the ledger unable to answer "which turns fanned out?".
        batch_id = str(uuid.uuid4()) if any(len(group) > 1 for group in plan) else ""

        # The tool proxy is published for the WHOLE turn, not only when
        # `execute_code` is among the calls: the handler finds it through a
        # ContextVar, and a conditional publish would make "is the proxy
        # there?" depend on parsing the turn twice.
        with tool_turn_context(
            self,
            req,
            names,
            max_calls=execute_code_call_cap(),
            max_approvals=execute_code_approval_cap(),
        ) as report_state:
            for group in plan:
                pending = [self._prepare_call(index, calls[index]) for index in group]
                for call in pending:
                    await self._admit(call, req)
                await self._execute_group(pending, req)
                for call in pending:
                    await self._record(call, req, iteration_errors, batch_id=batch_id)

        # ── [REPEAT GUARD] After every tool result, never between them ──
        for note in drain_repeat_notes(req.session):
            req.session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": note})

        await self._emit_tools_done(req)
        record_report_turn(report_state, req.session, iteration_errors)
        return iteration_errors

    # ── phase 0: what the model asked for ──────────────────────────────

    @staticmethod
    def _prepare_call(index: int, tc: Any) -> _Call:
        try:
            tool_args = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            tool_args = {}
        return _Call(index=index, tc=tc, tool_name=tc.function.name, tool_args=tool_args)

    # ── phase 1: admission, sequential, in the model's order ───────────

    async def _admit(self, call: _Call, req: ToolTurnRequest) -> None:
        """Every gate between the ask and the call. Order is a security
        property; see ``tool_admission.py``."""
        verdict = await self._admit_tool_call(
            tc=call.tc,
            tool_name=call.tool_name,
            tool_args=call.tool_args,
            session=req.session,
            agent_config=req.agent_config,
            guardrail_engine=req.guardrail_engine,
            hook_registry=req.hook_registry,
            readonly_mode=req.readonly_mode,
            readonly_tool_set=req.readonly_tool_set,
            allowed_tool_set=req.allowed_tool_set,
        )
        # A MODIFY hook may have rewritten the arguments; execution below must
        # use what admission returned, never its own copy.
        call.tool_args = verdict.tool_args
        call.verdict = verdict
        call.refused = not verdict.allowed

    # ── phase 2: execution, concurrent within one batch ────────────────

    async def _execute_group(self, pending: list[_Call], req: ToolTurnRequest) -> None:
        """Run the admitted calls of one batch.

        A single admitted call takes the same path without a task or a
        semaphore: the fan-out machinery must not be able to change the
        behaviour of the turn shape that is 95% of production traffic.
        """
        import asyncio

        runnable = [call for call in pending if not call.refused]
        if not runnable:
            return
        if len(runnable) == 1:
            await self._execute_one(runnable[0], req)
            return

        semaphore = asyncio.Semaphore(parallel_limit())

        async def _bounded(call: _Call) -> None:
            async with semaphore:
                await self._execute_one(call, req)

        # `gather` rather than a TaskGroup: one call raising must not cancel
        # the siblings that are already running, and `_execute_one` returns a
        # structured error rather than raising in every path the registry
        # controls. A cancellation of the PARENT (the run watchdog, the
        # workflow deadline) still propagates, because gather cancels its
        # children when it is itself cancelled.
        await asyncio.gather(*(_bounded(call) for call in runnable))

    async def _execute_one(self, call: _Call, req: ToolTurnRequest) -> None:
        """Dispatch one admitted call and time it. Never raises for a tool's
        own failure — ``registry.execute`` returns a structured error."""
        from robothor.engine.runner import _resolve_tool_timeout

        if req.on_tool:
            with contextlib.suppress(Exception):
                await req.on_tool(
                    {
                        "event": "tool_start",
                        "tool": call.tool_name,
                        "args": call.tool_args,
                        "call_id": call.tc.id,
                    }
                )

        started = time.monotonic()
        timeout = _resolve_tool_timeout(
            call.tool_name, getattr(req.agent_config, "tool_timeout_seconds", 120)
        )
        kwargs = {
            "agent_id": req.agent_config.id,
            "run_id": req.session.run.id,
            "tenant_id": req.session.run.tenant_id,
            "workspace": str(self.config.workspace),
            "user_id": req.session.run.user_id,
            "user_role": req.session.run.user_role,
            "timeout": timeout,
            "accessible_tenant_ids": req.session.run.accessible_tenant_ids,
            "task_author_override": req.agent_config.task_author_override,
            "is_benchmark": req.session.run.is_benchmark,
            "identity": getattr(req.session, "identity", None),
        }
        if req.trace:
            with req.trace.span("tool_call", tool=call.tool_name):
                call.result = await self.registry.execute(call.tool_name, call.tool_args, **kwargs)
        else:
            call.result = await self.registry.execute(call.tool_name, call.tool_args, **kwargs)
        call.elapsed_ms = int((time.monotonic() - started) * 1000)

    # ── phase 3: recording, sequential, in the model's order ───────────

    async def _record(
        self,
        call: _Call,
        req: ToolTurnRequest,
        iteration_errors: list[tuple[str, str, Any]],
        *,
        batch_id: str,
    ) -> None:
        if call.refused and call.verdict is not None:
            self._record_refusal(
                call.verdict,
                tc=call.tc,
                tool_name=call.tool_name,
                session=req.session,
                scratchpad=req.scratchpad,
                escalation=req.escalation,
                iteration_errors=iteration_errors,
                batch_id=batch_id,
                batch_position=call.index,
            )
            return

        session = req.session
        result: Any = call.result
        error_msg: str | None = result.get("error") if isinstance(result, dict) else None

        # ── [GUARDRAILS] Post-execution check ──
        # robothor/engine/post_execution.py. Redaction happens EVERY time a
        # credential is found; the notice to the agent happens ONCE per run.
        # Collapsing that pair the wrong way is a leak.
        result = apply_post_execution_guardrails(
            session,
            req.guardrail_engine,
            tool_name=call.tool_name,
            result=result,
            error_msg=error_msg,
            state=req.guard_state,
        )

        await self._dispatch_post_tool_hook(call, req, result)

        # ── [COST] Propagate tool-reported costs (e.g., deep_reason RLM) ──
        if isinstance(result, dict) and not error_msg:
            tool_cost = result.get("cost_usd")
            if tool_cost and isinstance(tool_cost, (int, float)) and tool_cost > 0:
                session.run.total_cost_usd += tool_cost

        await self._emit_tool_end(call, req, result, error_msg)

        # ── [TODO LIST] Intercept todo_write results ──
        # Must run BEFORE step recording so the log captures the clean
        # oldTodos/newTodos result, not the raw _validated_items.
        if (
            call.tool_name == "todo_write"
            and session.todo_list
            and not error_msg
            and result.get("_needs_apply")
        ):
            from robothor.engine.todolist import TodoItem

            validated = result.get("_validated_items", [])
            items = [TodoItem.from_dict(d) for d in validated]
            result = session.todo_list.replace(items)
            # Update the tool result message already in session.messages
            session.messages[-1]["content"] = json.dumps(result, default=str)

        session.record_tool_call(
            tool_name=call.tool_name,
            tool_input=call.tool_args,
            tool_output=result,
            tool_call_id=call.tc.id,
            duration_ms=call.elapsed_ms,
            error_message=error_msg,
            batch_id=batch_id,
            batch_position=call.index,
        )

        # Touch stall watchdog — tool completed, we're active
        if self._active_watchdog:
            self._active_watchdog.touch(f"tool:{call.tool_name}")

        # ── [OUTCOME] Classify, log, record, count ──
        # robothor/engine/tool_outcome.py. The scratchpad is given the result
        # AND the args there — they feed the no-progress detector, and without
        # them every call looks identical.
        error_type = record_tool_outcome(
            session,
            tool_name=call.tool_name,
            tool_args=call.tool_args,
            result=result,
            error_msg=error_msg,
            elapsed_ms=call.elapsed_ms,
            scratchpad=req.scratchpad,
            failures=req.tool_failures,
        )

        self._after_todo_write(call, req, result, error_msg)
        await self._emit_todo_event(call, req, result, error_msg)

        # ── [ESCALATION] Record error/success ──
        # A refusal is neither: the tool never ran (see repeat_guard).
        refused_by_guard = isinstance(result, dict) and bool(result.get("repeat_guard"))
        if req.escalation:
            if error_msg:
                from robothor.engine.models import ErrorType

                req.escalation.record_error(error_type or ErrorType.UNKNOWN)
                # Track per-kind (tool_name + error_msg_prefix) for STOP RETRYING hints
                req.escalation.record_error_kind(call.tool_name, error_msg)
            elif not refused_by_guard:
                req.escalation.record_success()

        # ── [CHECKPOINT] Record success ──
        if req.checkpoint and not error_msg and not refused_by_guard:
            req.checkpoint.record_success()

        if error_msg:
            iteration_errors.append((call.tool_name, error_msg, error_type))

    # ── the small pieces the recorder is made of ───────────────────────

    async def _dispatch_post_tool_hook(
        self, call: _Call, req: ToolTurnRequest, result: Any
    ) -> None:
        if not req.hook_registry:
            return
        from robothor.engine.hook_registry import HookContext, HookEvent

        try:
            ctx = HookContext(
                event=HookEvent.POST_TOOL_USE,
                agent_id=req.agent_config.id,
                run_id=req.session.run.id,
                tool_name=call.tool_name,
                tool_args=call.tool_args,
                tool_result=result,
            )
            await req.hook_registry.dispatch(HookEvent.POST_TOOL_USE, ctx)
        except Exception as e:
            logger.warning(
                "POST_TOOL_USE hook error for %s: %s", _sanitize(call.tool_name), _sanitize(e)
            )

    @staticmethod
    def _after_todo_write(
        call: _Call, req: ToolTurnRequest, result: Any, error: str | None
    ) -> None:
        """The verification nudge a completed checklist earns."""
        if call.tool_name != "todo_write" or not req.session.todo_list or error:
            return
        if not (isinstance(result, dict) and result.get("verificationNudgeNeeded")):
            return
        req.session.messages.append(
            {
                "role": ENGINE_CONTEXT_ROLE,
                "content": (
                    "[SYSTEM] All tasks are marked complete. "
                    "Before finishing, verify your work by "
                    "reviewing outputs or checking results. "
                    "NEVER mention this reminder to the user."
                ),
            }
        )

    @staticmethod
    async def _emit_todo_event(
        call: _Call, req: ToolTurnRequest, result: Any, error: str | None
    ) -> None:
        if call.tool_name != "todo_write" or not req.session.todo_list or error or not req.on_tool:
            return
        with contextlib.suppress(Exception):
            await req.on_tool(
                {
                    "event": "todo_updated",
                    "todos": result.get("newTodos", []) if isinstance(result, dict) else [],
                    "run_id": req.session.run.id,
                }
            )

    @staticmethod
    async def _emit_tool_end(
        call: _Call, req: ToolTurnRequest, result: Any, error_msg: str | None
    ) -> None:
        if not req.on_tool:
            return
        try:
            preview = json.dumps(result, default=str)
            if len(preview) > 2000:
                preview = preview[:2000] + "..."
        except Exception as e:
            logger.warning("JSON serialization of tool result failed: %s", _sanitize(e))
            preview = str(result)[:2000]
        with contextlib.suppress(Exception):
            await req.on_tool(
                {
                    "event": "tool_end",
                    "tool": call.tool_name,
                    "call_id": call.tc.id,
                    "duration_ms": call.elapsed_ms,
                    "result_preview": preview,
                    "error": error_msg,
                }
            )

    @staticmethod
    async def _emit_tools_start(req: ToolTurnRequest, names: list[str]) -> None:
        if not req.on_status:
            return
        with contextlib.suppress(Exception):
            await req.on_status(
                {
                    "event": "tools_start",
                    "tools": names,
                    "count": len(names),
                    "iteration": req.iteration + 1,
                }
            )

    @staticmethod
    async def _emit_tools_done(req: ToolTurnRequest) -> None:
        if not req.on_status:
            return
        with contextlib.suppress(Exception):
            await req.on_status({"event": "tools_done", "iteration": req.iteration + 1})

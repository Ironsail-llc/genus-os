"""Admission control: everything that stands between a model asking for a
tool and the tool running.

Five gates, in a fixed order — plan mode, the agent's built tool set, the
PRE_TOOL_USE lifecycle hook, the guardrail engine (including human approval),
and the system-run RBAC check. Order is a security property: the cheapest and
most absolute answers first, and a later gate never gets the chance to
approve what an earlier one refused.

These lived inline in ``_run_loop`` as ~310 lines, and the reason to pull them
out is not line count. Every gate ended with the same five-statement refusal
tail — record the step, count the error, tell the scratchpad, maybe count the
escalation, continue — and the tails were NOT identical. Three differences had
accumulated where nobody could see them:

* Plan-mode and tools_allowed refusals do not count toward escalation; hook,
  guardrail, and RBAC refusals do.
* The two human-approval denials count toward neither escalation nor the
  iteration's error list, so they never trigger error feedback to the model.
* Only guardrail and RBAC refusals write an ``agent_guardrail_events`` row.

Whether each of those is right is a separate question from whether it is
visible. ``ToolVerdict`` makes them named fields at every call site, so a
future change to any of them is a decision instead of an accident.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.engine.sanitize import sanitize_log as _sanitize

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig
    from robothor.engine.session import AgentSession

logger = logging.getLogger(__name__)


@dataclass
class ToolVerdict:
    """What admission control decided about one tool call.

    ``allowed`` is the only field the happy path reads; the rest describe how
    a refusal should be recorded, and exist so the differences between gates
    are visible rather than buried in each gate's copy of the refusal tail.
    """

    allowed: bool
    #: What the model is told. Also what lands in the step's error_message.
    message: str = ""
    #: Merged into the recorded tool_output, e.g. {"guard": "plan_mode"} —
    #: this is how an operator reading the ledger later knows which control
    #: fired, without parsing the message text.
    output: dict[str, Any] = field(default_factory=dict)
    #: Whether this refusal counts toward the escalation threshold.
    escalate: bool = False
    #: Whether this refusal joins the iteration's error list — which drives
    #: error feedback to the model and the recovery machinery.
    count_as_iteration_error: bool = True
    #: Possibly rewritten by a MODIFY hook; the caller uses this, not its own.
    tool_args: dict[str, Any] = field(default_factory=dict)


def _system_trigger_types() -> frozenset[Any]:
    """The runner's own allowlist, imported rather than restated.

    A second copy of this set is the parallel-list drift that has bitten this
    codebase repeatedly, and here the drift would be a silent security
    change: a trigger type present in one copy and absent from the other
    either skips the RBAC gate or applies it where it never applied before.
    """
    from robothor.engine.runner import _SYSTEM_TRIGGER_TYPES

    return _SYSTEM_TRIGGER_TYPES


def _log_guardrail_event(**kwargs: Any) -> None:
    """Audit a control that fired. Never raises.

    A control firing and leaving no trace is itself an incident, so the
    failure to record is logged at ERROR rather than swallowed — but it must
    not be able to stop the control it is recording.
    """
    try:
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.error("guardrail event could not be recorded: %s", _sanitize(exc))


class ToolAdmissionMixin:
    """Tool-call admission control for AgentRunner."""

    if TYPE_CHECKING:
        # Mirrors of what the host class provides. Real signatures, not
        # `(*a, **k) -> Any`: a lazy stub here would type-launder every call
        # below and defeat the point of having the contract at all.
        _active_watchdog: Any

    async def _admit_tool_call(
        self,
        *,
        tc: Any,
        tool_name: str,
        tool_args: dict[str, Any],
        session: AgentSession,
        agent_config: AgentConfig,
        guardrail_engine: Any,
        hook_registry: Any,
        readonly_mode: bool,
        readonly_tool_set: frozenset[str],
        allowed_tool_set: frozenset[str],
    ) -> ToolVerdict:
        """Run every gate in order and return the first refusal, or an allow."""
        # ── [PLAN MODE GUARD] Runtime enforcement ──
        # Belt-and-suspenders: even though schemas are filtered, block any
        # non-readonly tool call during plan mode at runtime.
        if readonly_mode and tool_name not in readonly_tool_set:
            return ToolVerdict(
                allowed=False,
                message=(
                    f"Tool '{tool_name}' is not available in plan mode. "
                    "Only read-only tools can be used during planning."
                ),
                output={"guard": "plan_mode"},
                tool_args=tool_args,
            )

        # ── [TOOLS_ALLOWED GUARD] Runtime enforcement ──
        # Belt-and-suspenders: even though schemas are filtered, block any
        # tool call not in the agent's allowed set.
        if allowed_tool_set and tool_name not in allowed_tool_set:
            return ToolVerdict(
                allowed=False,
                message=f"Tool '{tool_name}' is not available to this agent.",
                output={"guard": "tools_allowed"},
                tool_args=tool_args,
            )

        # ── [HOOKS] Pre-tool-use lifecycle hook ──
        if hook_registry:
            from robothor.engine.hook_registry import HookAction, HookContext, HookEvent

            try:
                pre_tool_ctx = HookContext(
                    event=HookEvent.PRE_TOOL_USE,
                    agent_id=agent_config.id,
                    run_id=session.run.id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                )
                pre_hr = await hook_registry.dispatch(HookEvent.PRE_TOOL_USE, pre_tool_ctx)
                if pre_hr.action == HookAction.BLOCK:
                    return ToolVerdict(
                        allowed=False,
                        message=f"Blocked by lifecycle hook: {pre_hr.reason}",
                        output={"hook": "pre_tool_use"},
                        escalate=True,
                        tool_args=tool_args,
                    )
                if pre_hr.action == HookAction.MODIFY and pre_hr.modified_args:
                    tool_args = pre_hr.modified_args
            except Exception as e:
                # A hook that raises is a broken hook, not a denial. Failing
                # closed here would let one bad third-party hook halt every
                # tool call on the box.
                logger.warning(
                    "PRE_TOOL_USE hook error for %s: %s", _sanitize(tool_name), _sanitize(e)
                )

        # ── [GUARDRAILS] Pre-execution check ──
        if guardrail_engine:
            verdict = await self._check_guardrails(
                tc=tc,
                tool_name=tool_name,
                tool_args=tool_args,
                session=session,
                agent_config=agent_config,
                guardrail_engine=guardrail_engine,
            )
            if verdict is not None:
                return verdict

        # ── [RBAC] System-run permission gate ──
        # Only genuinely autonomous, no-interactive-user runs are governed by
        # the (permissive) service_role here. Interactive surfaces
        # (telegram/webchat/slack/ide/manual/webhook/channel) are gated by the
        # dispatch user_role check instead.
        if agent_config is not None and session.run.trigger_type in _system_trigger_types():
            from robothor.engine.feature_flags import rbac_enforcement_mode
            from robothor.engine.permissions import classify_system_tool_access

            rbac_mode = rbac_enforcement_mode()
            # classify_system_tool_access opens a sync DB connection; keep it
            # off the event loop so a slow round-trip can't stall the engine.
            rbac_action, rbac_reason = await asyncio.to_thread(
                classify_system_tool_access,
                agent_config.service_role,
                session.run.tenant_id,
                tool_name,
                rbac_mode,
            )
            if rbac_action != "allow":
                _log_guardrail_event(
                    run_id=session.run.id,
                    guardrail_name="rbac",
                    action="blocked" if rbac_action == "block" else "observed",
                    tool_name=tool_name,
                    reason=rbac_reason,
                    mode=rbac_mode,
                    step_number=len(session.run.steps),
                )
                if rbac_action == "block":
                    return ToolVerdict(
                        allowed=False,
                        message=f"Blocked by RBAC: {rbac_reason}",
                        output={"guardrail": "rbac"},
                        escalate=True,
                        tool_args=tool_args,
                    )

        return ToolVerdict(allowed=True, tool_args=tool_args)

    async def _check_guardrails(
        self,
        *,
        tc: Any,
        tool_name: str,
        tool_args: dict[str, Any],
        session: AgentSession,
        agent_config: AgentConfig,
        guardrail_engine: Any,
    ) -> ToolVerdict | None:
        """The guardrail gate, including human approval. None = allowed."""
        gr = guardrail_engine.check_pre_execution(
            tool_name,
            tool_args,
            agent_id=agent_config.id,
            prior_steps=session.run.steps,
        )

        # ── [OBSERVE] Allowed, but a rollout-gated guardrail would have
        # blocked this in enforce mode. Persist it: a soak that records
        # nothing cannot distinguish "clean" from "blind".
        if gr.allowed and gr.action == "observed":
            _log_guardrail_event(
                run_id=session.run.id,
                guardrail_name=gr.guardrail_name,
                action="observed",
                tool_name=tool_name,
                reason=gr.reason,
                mode="observe",
                step_number=len(session.run.steps),
            )
        if gr.allowed:
            return None

        # ── [HUMAN APPROVAL] Escalation for opt-in agents ──
        if gr.action == "escalate":
            from robothor.engine.permission_escalation import get_permission_manager

            mgr = get_permission_manager()
            if mgr:
                approved = await mgr.request_approval(
                    agent_id=agent_config.id,
                    run_id=session.run_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    guardrail_name=gr.guardrail_name,
                    reason=gr.reason,
                    timeout_seconds=agent_config.human_approval_timeout,
                )
                if approved:
                    return None
                # An operator saying no is not the agent erring: this denial
                # counts toward neither escalation nor error feedback.
                return ToolVerdict(
                    allowed=False,
                    message=f"Denied by operator ({gr.guardrail_name}): {gr.reason}",
                    count_as_iteration_error=False,
                    tool_args=tool_args,
                )
            if agent_config.human_approval_fail_open:
                return None  # opted-in unattended autonomy: auto-approve

            # No approver reachable. Legacy behavior auto-approves;
            # ROBOTHOR_APPROVAL_* makes this fail closed (observe logs the
            # would-deny; enforce denies the tool).
            from robothor.engine.feature_flags import approval_mode
            from robothor.engine.permission_escalation import fail_closed_on_missing_manager

            appr_mode = approval_mode()
            if appr_mode != "off":
                _log_guardrail_event(
                    run_id=session.run.id,
                    guardrail_name=gr.guardrail_name,
                    action="blocked" if appr_mode == "enforce" else "observed",
                    tool_name=tool_name,
                    reason="human approval required but no approver reachable",
                    mode=appr_mode,
                    step_number=len(session.run.steps),
                )
            if fail_closed_on_missing_manager():
                return ToolVerdict(
                    allowed=False,
                    message=(
                        f"Denied — human approval required for "
                        f"{gr.guardrail_name} but no approver is reachable"
                    ),
                    count_as_iteration_error=False,
                    tool_args=tool_args,
                )
            return None  # otherwise auto-approve (legacy) and fall through

        # Plain block.
        _log_guardrail_event(
            run_id=session.run.id,
            guardrail_name=gr.guardrail_name,
            action="blocked",
            tool_name=tool_name,
            reason=gr.reason,
            mode="enforce",
            step_number=len(session.run.steps),
        )
        try:
            from robothor.engine.tracking import log_tool_event

            log_tool_event(
                run_id=session.run.id,
                tool_name=tool_name,
                duration_ms=0,
                success=False,
                error_type="guardrail_blocked",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("tool event could not be recorded: %s", _sanitize(exc))

        return ToolVerdict(
            allowed=False,
            message=f"Blocked by guardrail ({gr.guardrail_name}): {gr.reason}",
            output={"guardrail": gr.guardrail_name},
            escalate=True,
            tool_args=tool_args,
        )

    @staticmethod
    def _record_refusal(
        verdict: ToolVerdict,
        *,
        tc: Any,
        tool_name: str,
        session: AgentSession,
        scratchpad: Any,
        escalation: Any,
        iteration_errors: list[tuple[str, str, Any]],
    ) -> None:
        """The refusal tail every gate used to carry its own copy of."""
        session.record_tool_call(
            tool_name=tool_name,
            tool_input=verdict.tool_args,
            tool_output={"error": verdict.message, **verdict.output},
            tool_call_id=tc.id,
            error_message=verdict.message,
        )
        if verdict.count_as_iteration_error:
            iteration_errors.append((tool_name, verdict.message, None))
        if scratchpad:
            scratchpad.record_tool_call(tool_name, error=verdict.message)
        if verdict.escalate and escalation:
            escalation.record_error()

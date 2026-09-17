"""What happens to a tool call an agent's own manifest asked a human about.

Genus OS runs agents **autonomously**. This module is the opt-in half: it is
reached only when a guardrail returned ``action="escalate"``, which needs both
``v2.guardrails: [human_approval]`` and the tool named in
``v2.human_approval_tools`` in that agent's manifest. A default install has
neither, and nothing here runs.

Two answers, and the interesting one is the denial. From inside a turn the
agent cannot see its own manifest, so "Denied by operator" reads as a
transient refusal and the obvious recovery is to try again — which on an
unattended schedule means waiting out ``human_approval_timeout`` a second
time against a person who is not there. So every denial names the manifest key
that produced it: the run can then route around the tool instead of re-queuing
behind a prompt nobody will answer.

Extracted from ``tool_admission.py`` when that module reached its
decomposition cap. It is a cohesive cluster — one branch, one external
dependency (``permission_escalation``) — rather than a slice taken to make a
line count fit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.engine.models import AgentConfig
    from robothor.engine.session import AgentSession
    from robothor.engine.tool_admission import ToolVerdict

__all__ = ["GATE_IS_INSTANCE_CONFIG", "resolve_escalation"]

#: Appended to every human-approval denial the model reads.
#:
#: The gate is instance configuration, not a platform rule, and saying which
#: key turned it on is the difference between an agent that finds another route
#: and one that spends its budget retrying.
GATE_IS_INSTANCE_CONFIG = (
    "This gate is instance configuration — `v2.human_approval_tools` in this agent's "
    "manifest, not a platform rule — so a retry will be denied the same way: reach the "
    "goal by another route, or report this tool as unavailable and move on."
)


async def resolve_escalation(
    *,
    gr: Any,
    tool_name: str,
    tool_args: dict[str, Any],
    session: AgentSession,
    agent_config: AgentConfig,
) -> ToolVerdict | None:
    """``None`` to let the call through; a denying verdict to stop it.

    ``None`` covers three shapes deliberately: the operator approved, the agent
    opted into ``human_approval_fail_open``, and the legacy no-approver
    behaviour outside ``enforce``. All three mean "proceed", and the caller
    treats them identically.
    """
    from robothor.engine.permission_escalation import get_permission_manager
    from robothor.engine.tool_admission import ToolVerdict, _log_guardrail_event

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
        # An operator saying no is not the agent erring: this denial counts
        # toward neither escalation nor error feedback.
        return ToolVerdict(
            allowed=False,
            message=(
                f"Denied by operator ({gr.guardrail_name}): {gr.reason}. {GATE_IS_INSTANCE_CONFIG}"
            ),
            count_as_iteration_error=False,
            tool_args=tool_args,
        )
    if agent_config.human_approval_fail_open:
        return None  # opted-in unattended autonomy: auto-approve

    # No approver reachable. Legacy behavior auto-approves; ROBOTHOR_APPROVAL_*
    # makes this fail closed (observe logs the would-deny; enforce denies).
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
                f"Denied — human approval required for {gr.guardrail_name} but no "
                f"approver is reachable. {GATE_IS_INSTANCE_CONFIG}"
            ),
            count_as_iteration_error=False,
            tool_args=tool_args,
        )
    return None  # otherwise auto-approve (legacy) and fall through

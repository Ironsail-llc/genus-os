"""What admission control decided about one tool call, and the refusal's gate.

``ToolVerdict`` lives here rather than in ``tool_admission`` so the gate and
unlock for each refusal can be derived in one place without growing the
admission module: :func:`with_gate` reads the legacy ``output`` key every
admission refusal already records (``guard``, ``hook``, ``guardrail``) and adds
the gate, its name and the unlock (see ``robothor.engine.refusal``). A verdict
with none of those keys -- an allow, a human-approval denial -- is returned
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from robothor.engine.refusal import refusal_fields

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

__all__ = ["ToolVerdict", "with_gate"]


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
    #: Which kind of control refused (``robothor.engine.refusal.GATES``) and what
    #: a person changes for the call to succeed. Filled by :func:`with_gate`; they
    #: reach the model's tool result only with ``ROBOTHOR_REFUSAL_PAYLOAD_V2``.
    gate: str = ""
    unlock: str = ""


def _not_in_schemas(tool: str, config: AgentConfig | None) -> tuple[str, str, str]:
    """A call to a tool outside this turn's schemas: deferred, or not granted.

    The run's deferred allow-set is the set ``tool_call`` admits, so a tool in
    it is granted and only needs calling that way; anything else is a manifest
    change.
    """
    from robothor.engine.capabilities import manifest_unlock
    from robothor.engine.tools.dispatch import get_deferred_allowed

    if tool not in (get_deferred_allowed() or ()):
        return "allowlist", "tools_allowed", manifest_unlock(config, tool)
    return (
        "policy",
        "deferred",
        (
            f"'{tool}' is granted but deferred on this run: call it through tool_call "
            f"with name '{tool}' and its arguments (tool_describe gives its schema); "
            "nothing needs to change and no one needs to be asked"
        ),
    )


def _gate_for(verdict: ToolVerdict, tool: str, config: AgentConfig | None) -> tuple[str, str, str]:
    """``(gate, gate_name, unlock)`` for an admission refusal, or empty strings."""
    out = verdict.output
    guard, guardrail = out.get("guard"), out.get("guardrail")
    if guard == "plan_mode":
        return (
            "policy",
            "plan_mode",
            (
                f"finish the plan and ask the person to approve it; once they leave plan "
                f"mode, '{tool}' runs"
            ),
        )
    if guard == "tools_allowed":
        return _not_in_schemas(tool, config)
    if guard == "run_wrapup":
        return (
            "policy",
            "run_wrapup",
            (
                f"this run's time is nearly up: write the answer now, and make the '{tool}' "
                "call in the next run"
            ),
        )
    if out.get("hook") == "pre_tool_use":
        return (
            "policy",
            "pre_tool_use",
            (
                "a pre-tool-use hook refused this call and its reason says what it needs; "
                "an operator can change or remove the hook"
            ),
        )
    if guardrail == "rbac":
        role = getattr(config, "service_role", "") or "service"
        return (
            "role",
            "rbac",
            (
                f"this scheduled or system run acts as service role '{role}', which may not use "
                f"'{tool}'. An owner or admin can grant that role the tool (a role_permissions "
                "allow row), or run it from a chat by someone whose role allows it"
            ),
        )
    if guardrail:
        agent = getattr(config, "id", "") or "<agent>"
        return (
            "policy",
            str(guardrail),
            (
                f"the '{guardrail}' guardrail refused this call for the reason given. Change "
                f"the call to satisfy it, or an operator can change the agent's guardrails in "
                f"docs/agents/{agent}.yaml"
            ),
        )
    return "", "", ""


def with_gate(verdict: ToolVerdict, tool: str, config: AgentConfig | None) -> ToolVerdict:
    """``verdict`` with its gate and unlock filled, and the v2 keys in ``output``."""
    if verdict.allowed:
        return verdict
    gate, name, unlock = _gate_for(verdict, tool, config)
    if not gate:
        return verdict
    extra = refusal_fields(gate=gate, name=name, unlock=unlock)
    return replace(verdict, output={**verdict.output, **extra}, gate=gate, unlock=unlock)

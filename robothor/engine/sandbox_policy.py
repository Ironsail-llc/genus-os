"""Who gets contained, and who is allowed to say no.

Extracted from `runner.py`, which is a 2,957-line god-object held at a
decomposition ratchet. Moving a cohesive cluster out is the documented remedy;
raising the cap by the size of each fix is how a ratchet stops being one.

The finding this module exists to make visible, measured 2026-08-27:

    6 agents hold `exec`
    4 of them declare `sandbox: host` — main, crm-hygiene,
      conversation-inbox, vision-monitor
    the opt-out is honoured BEFORE the mode is consulted

So promoting `ROBOTHOR_SANDBOX_DEFAULT_MODE` to `enforce` would containerise
`auto-agent` and `email-analyst` and nothing else, while the operator
reasonably believes they have just contained their fleet. The dashboard would
say `enforce` and be telling the truth.

The opt-out stays — some agents genuinely need the host, and silently
containerising `main` would be a worse failure than not containerising it. What
changes is that the opt-out is stated, countable, and overridable, so a
promotion can be argued from a number instead of an assumption.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from robothor.engine.sandbox import RuntimeStatus
from robothor.engine.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

#: Lets `enforce` override a manifest's `sandbox: host`. Deliberately not the
#: default: an operator who has not read the opt-out list should not discover
#: it by having `main` start failing inside a container.
OVERRIDE_OPT_OUT_ENV = "ROBOTHOR_SANDBOX_ENFORCE_OVERRIDES_MANIFEST"

#: One warning per agent per process, not per run.
_warned: set[str] = set()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def agent_holds_exec(config: Any) -> bool:
    """True if the agent can call ``exec`` (i.e. touches the host shell).

    An empty ``tools_allowed`` means the agent receives the full tool set
    (including ``exec``); a ``tools_denied`` entry removes it.
    """
    denied = set(getattr(config, "tools_denied", None) or [])
    if "exec" in denied:
        return False
    allowed = getattr(config, "tools_allowed", None) or []
    return "exec" in allowed or not allowed


def resolve_sandbox_decision(config: Any, mode: str, *, agent_id: str = "") -> str:
    """``'docker'`` | ``'observe'`` | ``'host'`` for one run.

    ``observe`` means an exec-holding agent that WOULD be contained but runs on
    the host; the caller logs it. Observe must never change behaviour — that is
    the entire point of the mode — so the override below applies only under
    ``enforce``. An override that quietly contained agents during observe would
    make the would-block set a record of the override rather than of the fleet.
    """
    declared = getattr(config, "sandbox", "") or ""

    if declared == "docker":
        return "docker"

    holds_exec = agent_holds_exec(config)

    if declared == "host":
        # Only worth mentioning when it is actually bypassing something: an
        # agent with no `exec` is not weakening anything by asking for the
        # host, and warning about it would be noise.
        if holds_exec and mode in ("observe", "enforce"):
            _warn_opt_out(agent_id or getattr(config, "id", "") or "<unknown>", mode)
        if not (holds_exec and _truthy(OVERRIDE_OPT_OUT_ENV)):
            return "host"
        # With the override set, an opted-out agent is in scope again.
        # Under observe it is REPORTED but still runs on the host: that does
        # not change behaviour (both run on the host either way), and it is
        # what tells the operator which agents the override would newly
        # capture before they flip enforce. Promoting on a would-block set
        # that silently excluded them is how you contain two agents out of six
        # and believe you contained the fleet.
        return "docker" if mode == "enforce" else "observe"

    if mode != "off" and holds_exec:
        return "docker" if mode == "enforce" else "observe"
    return "host"


def _warn_opt_out(agent_id: str, mode: str) -> None:
    if agent_id in _warned:
        return
    _warned.add(agent_id)
    logger.warning(
        "Agent %s holds `exec` and declares `sandbox: host`, so it opts out of "
        "containment. sandbox_default is in %s mode; this agent is unaffected "
        "by it either way. Set %s=1 to make enforce override the manifest.",
        # Agent ids come from manifest files, so a newline in one could forge a
        # second log line saying whatever it liked about containment.
        sanitize_log(agent_id),
        mode,
        OVERRIDE_OPT_OUT_ENV,
    )


def opted_out_of_containment(manifests: Iterable[dict[str, Any]]) -> list[str]:
    """Agent ids that hold ``exec`` and decline the sandbox.

    One number for the readiness probe, the dashboard and any promotion
    argument, rather than three callers each re-deriving it slightly
    differently — which is how a hand-maintained agent-name list drifted from
    what was registered and produced three separate bug reports for one defect.
    """
    out = []
    for manifest in manifests:
        if manifest.get("sandbox") != "host":
            continue
        shim = type(
            "_M",
            (),
            {
                "tools_allowed": manifest.get("tools_allowed", []),
                "tools_denied": manifest.get("tools_denied", []),
            },
        )()
        if agent_holds_exec(shim):
            out.append(str(manifest.get("id", "")))
    return out


# ── The sandbox as a playground (ROBOTHOR_SANDBOX_PLAYGROUND) ─────────────
#
# The legacy decision above still says WHEN a container is wanted. What it
# never said is what happens when one is wanted and the engine has no runtime
# to start it: the run was failed at step 0, the owner included. Under the
# playground the run continues and this plan says where each `exec` goes. An
# agent the legacy ladder leaves on the host stays there, because a
# `--network none` container round an agent that never asked for one would
# block work that runs today.
#
# What DOES widen, and only for an owner or admin driving the run themselves
# (``acting_host_role``): the exec allowlist does not bind them, and
# `execute_code` runs on the engine host even beside a live container. Both
# are recorded. A member, an automated run and a sub-agent are not widened.

#: Roles whose own permissions may stand in for a missing container. The
#: human's role is the limit, not the infrastructure.
HOST_ROLES: frozenset[str] = frozenset({"owner", "admin"})

#: Triggers with no person on the other end: the runner's system triggers
#: (``runner._SYSTEM_TRIGGER_TYPES``, pinned equal by a test). Their role is
#: the agent's service role, which a manifest may set to "owner".
NO_PERSON_TRIGGERS: frozenset[str] = frozenset(
    {"cron", "hook", "event", "workflow", "sub_agent", "federation", "channel_event"}
)

#: Stands in for a probe that was not needed: no container is wanted.
_NOT_PROBED = RuntimeStatus(binary="", available=False, reason="not probed")

#: Plans kept for this many runs. Keyed by run id rather than a ContextVar: a
#: spawned sub-agent runs inline in its parent's context, so a ContextVar it
#: set would leak back into the parent when it returned.
_MAX_PLANS = 4096

#: How a degraded or refused plan reads in its ``sandbox_playground`` row.
_OUTCOME = {"host": "run on the host", "refuse": "be refused", "sandbox": "run in the sandbox"}

#: Where the live run carries its plan (see ``exec_plan_for``).
_RUN_ATTR = "_exec_plan"

_INSTALL = (
    "the operator can install a sandbox runtime (rootless podman) on the engine "
    "or select one with ROBOTHOR_SANDBOX_BACKEND."
)
_UNLOCK = f"An owner or admin can run it, or {_INSTALL}"
#: A sub-agent's requester may already be the owner, so the role is not the
#: unlock: the host fallback belongs to the run nothing spawned.
_SUB_AGENT_UNLOCK = (
    "Host fallback is only for the top-level run: have the parent agent run the "
    f"command itself, or {_INSTALL}"
)


def manifest_sandbox(manifest: dict[str, Any]) -> str:
    """A raw manifest's sandbox setting, read where the engine reads it.

    ``engine/config.py`` builds ``AgentConfig.sandbox`` from ``v2.sandbox``
    (default ``local``); a top-level ``sandbox:`` key is ignored there, so any
    reader of a raw manifest that wants the engine's answer must ignore it too.
    """
    v2 = manifest.get("v2") or {}
    return str(v2.get("sandbox", "local") if isinstance(v2, dict) else "local")


@dataclass(frozen=True)
class ExecPlan:
    """Where one run's ``exec`` goes: ``sandbox`` | ``host`` | ``refuse``.

    ``degraded`` is true when a container was wanted and the command will run on
    the host instead; ``reason`` and ``unlock`` are written to be repeated to the
    person on the other end, so they name the gate and what opens it.
    """

    where: Literal["sandbox", "host", "refuse"]
    reason: str = ""
    unlock: str = ""
    degraded: bool = False
    user_role: str = ""

    @property
    def host_note(self) -> str:
        """What a degraded host exec tells the agent, and the agent the owner."""
        return (
            f"Ran on the engine host ({self.reason}), under your {self.user_role} "
            "permissions. To run this in an isolated sandbox instead, the engine "
            "needs podman or a configured sandbox backend."
        )


def role_may_run_on_host(user_role: str) -> bool:
    """True for the roles that may run a contained command on the host."""
    return (user_role or "").strip().lower() in HOST_ROLES


def resolve_execution_plan(
    config: Any,
    mode: str,
    *,
    agent_id: str = "",
    user_role: str = "",
    runtime: RuntimeStatus,
    is_root_run: bool = True,
    is_benchmark: bool = False,
    person_drives: bool = True,
) -> ExecPlan:
    """The plan for one run. Pure: the runtime is probed by the caller.

    ``person_drives`` is false for a run with no person behind it; its role is
    then a service role and never earns the host fallback.
    """
    role = (user_role or "").strip().lower()
    if resolve_sandbox_decision(config, mode, agent_id=agent_id) != "docker":
        return ExecPlan(where="host", user_role=role)
    if runtime.available:
        return ExecPlan(where="sandbox", user_role=role)

    missing = runtime.reason or f"{runtime.binary} is not available"
    if not is_root_run:
        why = (
            f"there is no sandbox runtime on this engine ({missing}), and a "
            "sub-agent never falls back to the host"
        )
        return ExecPlan(where="refuse", reason=why, unlock=_SUB_AGENT_UNLOCK, user_role=role)
    if is_benchmark:
        why = (
            f"there is no sandbox runtime on this engine ({missing}), and a "
            "benchmark run never falls back to the host"
        )
        return ExecPlan(where="refuse", reason=why, unlock=_UNLOCK, user_role=role)
    if role in HOST_ROLES and person_drives:
        return ExecPlan(
            where="host",
            reason=f"no container runtime could be used on this instance: {missing}",
            degraded=True,
            user_role=role,
        )
    who = (
        role if person_drives else ""
    ) or "none: this is an automated run with no person behind it"
    why = (
        f"there is no sandbox runtime on this engine ({missing}) and your role "
        f"({who}) does not allow host execution"
    )
    return ExecPlan(where="refuse", reason=why, unlock=_UNLOCK, user_role=role)


_plans: OrderedDict[str, ExecPlan] = OrderedDict()


def set_exec_plan(run_id: str, plan: ExecPlan) -> None:
    """Record the plan for one run (bounded; the oldest is forgotten first)."""
    if not run_id:
        return
    _plans[run_id] = plan
    _plans.move_to_end(run_id)
    while len(_plans) > _MAX_PLANS:
        _plans.popitem(last=False)


def exec_plan_for(run_id: str) -> ExecPlan | None:
    """The plan for this run, or None (flag off, or no plan made).

    The live session's run carries it for the run's whole lifetime; the bounded
    table covers callers with no registered session. A busy instance starts more
    runs than the table holds, and a refused plan that fell out of it would let
    a member's exec run on the host.
    """
    if not run_id:
        return None
    from robothor.engine.session_registry import lookup

    live = getattr(getattr(lookup(run_id), "run", None), _RUN_ATTR, None)
    if isinstance(live, ExecPlan):
        return live
    return _plans.get(run_id)


async def establish_execution_plan(
    config: Any,
    mode: str,
    *,
    agent_id: str,
    run: Any,
    start_error: str = "",
) -> ExecPlan:
    """Probe the runtime (off the loop), decide, record, and say so when it matters.

    ``start_error`` is a container that was tried and failed: the runtime is
    treated as unavailable for this run, with that failure as the reason.
    """
    import asyncio

    from robothor.engine import sandbox as _sandbox

    if start_error:
        runtime = RuntimeStatus(
            binary=_sandbox.sandbox_binary(),
            available=False,
            reason=f"the container could not be started: {start_error}",
        )
    elif resolve_sandbox_decision(config, mode, agent_id=agent_id) != "docker":
        runtime = _NOT_PROBED  # no container wanted: the plan never reads it
    else:
        runtime = await asyncio.to_thread(_sandbox.runtime_status)
    plan = resolve_execution_plan(
        config,
        mode,
        agent_id=agent_id,
        user_role=getattr(run, "user_role", "") or "",
        runtime=runtime,
        is_root_run=is_root_run(run),
        is_benchmark=bool(getattr(run, "is_benchmark", False)),
        person_drives=person_drives(run),
    )
    run_id = str(getattr(run, "id", "") or "")
    set_exec_plan(run_id, plan)
    setattr(run, _RUN_ATTR, plan)  # lives exactly as long as the run
    if plan.degraded or plan.where == "refuse":
        await asyncio.to_thread(
            _record,
            run_id,
            "sandbox_playground",
            "observed",
            f"exec will {_OUTCOME[plan.where]}: {plan.reason}",
        )
    return plan


def record_exec_location(
    run_id: str, where: str, *, degraded: bool = False, beside: bool = False, tool: str = "exec"
) -> None:
    """One ``exec_location`` row per exec. Blocking: call it off the loop.

    ``beside`` is an owner's snippet that ran on the host while the run's
    container was live: the one widening past a working container.
    """
    reason = f"executed_on={where}"
    if degraded:
        reason += " (container wanted, none available)"
    elif beside:
        reason += " (a live container was running; widened by the driving owner or admin)"
    _record(run_id, "exec_location", "allowed", reason, tool=tool)


def _record(run_id: str, name: str, action: str, reason: str, *, tool: str = "exec") -> None:
    try:
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id=run_id,
            guardrail_name=name,
            action=action,
            tool_name=tool,
            reason=reason,
            mode="playground",
            step_number=0,
        )
    except Exception as exc:  # noqa: BLE001
        # A control fired; losing its audit trail is itself an incident.
        logger.error("guardrail event %s could not be recorded: %s", name, sanitize_log(exc))


# ── Call sites, kept to one line each where the ratchets are tight ────────


def _playground_on() -> bool:
    from robothor.engine.feature_flags import sandbox_playground_enabled

    return sandbox_playground_enabled()


async def _not_handled(_error: str) -> bool:
    return False


async def playground_decision(
    config: Any, mode: str, decision: str, agent_id: str, run: Any
) -> tuple[str, Callable[[str], Awaitable[bool]]]:
    """The runner's sandbox step under the playground: ``(decision, degrade)``.

    Flag off: the decision is returned untouched and ``degrade`` handles
    nothing, so the start-failure path fails closed exactly as before. Flag on:
    with no runtime there is nothing to start (decision ``host``), and
    ``degrade(error)`` turns a start failure into a plan instead of a failed run.
    """
    if not _playground_on():
        return decision, _not_handled
    plan = await establish_execution_plan(config, mode, agent_id=agent_id, run=run)
    if plan.where == "refuse" and _confined_tools_ungated(config):
        return decision, _not_handled

    async def degrade(error: str) -> bool:
        failed = await establish_execution_plan(
            config, mode, agent_id=agent_id, run=run, start_error=error
        )
        return not (failed.where == "refuse" and _confined_tools_ungated(config))

    return ("host" if plan.where != "sandbox" else decision), degrade


def exec_refusal(run_id: str, *, tool: str = "exec") -> dict[str, Any] | None:
    """The result for a run whose plan refuses, or None to go ahead."""
    if not _playground_on():
        return None
    plan = exec_plan_for(run_id)
    if plan is None or plan.where != "refuse":
        return None
    return {
        "error": f"{tool} did not run: {plan.reason}.",
        "unlock": plan.unlock,
        "error_type": "exec_unavailable",
    }


async def stamp_location(
    result: dict[str, Any], where: str, run_id: str, *, tool: str = "exec"
) -> dict[str, Any]:
    """Say where one ``exec`` (or snippet) ran, and record it once. Flag off: untouched."""
    if not isinstance(result, dict) or not _playground_on():
        return result
    import asyncio

    plan = exec_plan_for(run_id)
    off_box = plan is not None and where != "sandbox"
    degraded = off_box and plan is not None and plan.degraded
    beside = off_box and plan is not None and plan.where == "sandbox"
    result["executed_on"] = where
    if degraded and plan is not None:
        result["execution_note"] = plan.host_note
    elif beside and plan is not None:
        result["execution_note"] = (
            f"Ran on the engine host, not in this run's live container, under your "
            f"{plan.user_role} permissions: {tool} cannot reach the tool proxy from inside it."
        )
    await asyncio.to_thread(
        record_exec_location, run_id, where, degraded=degraded, beside=beside, tool=tool
    )
    return result


def guardrail_context(session: Any) -> dict[str, Any]:
    """Extra ``check_pre_execution`` kwargs under the playground; {} when off."""
    if not _playground_on():
        return {}
    run = getattr(session, "run", None)
    context: dict[str, Any] = {"user_role": acting_host_role(run)}
    plan = exec_plan_for(str(getattr(run, "id", "") or ""))
    if plan is not None and plan.where == "refuse":
        context["host_refusal"] = f"{plan.reason}. {plan.unlock}"
    return context


def proxy_may_run_on_host(proxy: Any) -> bool:
    """execute_code under the playground: an owner or admin driving the run may
    run a snippet where the proxy is, the engine host. Nobody else, never off."""
    return _playground_on() and bool(acting_host_role(_proxy_run(proxy)))


def person_drives(run: Any) -> bool:
    """A person is on the other end of this run: an interactive trigger and no
    service credential. Fails closed when the run says nothing about it."""
    trigger = getattr(run, "trigger_type", "") or ""
    trigger = str(getattr(trigger, "value", trigger))
    user_id = str(getattr(run, "user_id", "") or "")
    return (
        bool(trigger) and trigger not in NO_PERSON_TRIGGERS and not user_id.startswith("service:")
    )


def acting_host_role(run: Any) -> str:
    """The owner or admin role of the person DRIVING this run, else "".

    Not the bare ``user_role``: an automated run carries the agent's service
    role, which a manifest may set to "owner", and a sub-agent inherits its
    requester's role though nobody is driving it.
    """
    role = (getattr(run, "user_role", "") or "").strip().lower()
    if run is None or role not in HOST_ROLES or not is_root_run(run) or not person_drives(run):
        return ""
    return role


def proxy_plan_refusal(proxy: Any) -> dict[str, Any] | None:
    """execute_code under a refused plan: the same refusal ``exec`` gets."""
    run = _proxy_run(proxy)
    return exec_refusal(str(getattr(run, "id", "") or ""), tool="execute_code")


def _proxy_run(proxy: Any) -> Any:
    return getattr(getattr(getattr(proxy, "_req", None), "session", None), "run", None)


def is_root_run(run: Any) -> bool:
    """A run no other run spawned.

    Not from ``parent_run_id`` alone: a child of a parent whose own row was
    never recorded (``tracking_disabled``) carries an empty one. A spawned
    child always has the SUB_AGENT trigger and a nesting depth of at least 1.
    """
    trigger = getattr(run, "trigger_type", "")
    return (
        not getattr(run, "parent_run_id", None)
        and int(getattr(run, "nesting_depth", 0) or 0) == 0
        and str(getattr(trigger, "value", trigger)) != "sub_agent"
    )


#: The tools besides ``exec`` a container confines. With no container, a
#: refused plan refuses these too, or they would reach the engine host.
CONFINED_TOOLS: frozenset[str] = frozenset({"browser"})
CONFINED_TOOL_PREFIXES: tuple[str, ...] = ("desktop_",)


def is_confined_tool(tool_name: str) -> bool:
    return tool_name in CONFINED_TOOLS or tool_name.startswith(CONFINED_TOOL_PREFIXES)


def _confined_tools_ungated(config: Any) -> bool:
    """True when the agent holds a confined tool and has no guardrail engine to
    refuse it per call -- then a refused plan must fail closed as before."""
    from robothor.engine.guardrails import compute_effective_guardrails

    denied = set(getattr(config, "tools_denied", None) or [])
    allowed = list(getattr(config, "tools_allowed", None) or [])
    holds = not allowed or any(is_confined_tool(t) and t not in denied for t in allowed)
    if not holds:
        return False
    effective = compute_effective_guardrails(
        list(getattr(config, "guardrails", None) or []),
        opt_out=bool(getattr(config, "guardrails_opt_out", False)),
    )
    return not effective

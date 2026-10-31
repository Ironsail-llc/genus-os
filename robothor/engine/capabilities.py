"""What an agent can do on this instance, and what gates the rest.

One pure function, :func:`assess`, answers for both readers:

* the agent itself, through the "What you can do here" block in its system
  prompt (:func:`capability_block`, behind ``ROBOTHOR_CAPABILITY_BLOCK_ENABLED``);
* the operator, through ``genus doctor --category capabilities --agent ID``.

Two answers from two pieces of code would disagree sooner or later, and an agent
told one thing while the doctor says another is the defect this exists to fix.

The prompt passes only SESSION-STABLE facts: the agent's grant and the engine's
settings. No clock, no vault read, no per-turn state, so the prompt prefix stays
byte-identical from one turn to the next. Facts the prompt leaves out (is a
GitHub token stored, is the Claude Code CLI installed) are ``None`` there and
are checked when the tool is called; the doctor, which may do I/O, fills them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from robothor.engine.tools.constants import MAIL_REPLY_TOOL, MAIL_SEND_TOOL

if TYPE_CHECKING:
    from collections.abc import Iterable

    from robothor.engine.models import AgentConfig

__all__ = [
    "Capability",
    "Facts",
    "assess",
    "capability_block",
    "facts_for",
    "manifest_unlock",
    "render",
]

HEADER = "## What you can do here"


@dataclass(frozen=True)
class Facts:
    """Everything :func:`assess` reads besides the granted tool names."""

    agent_id: str
    service_role: str = "service"
    #: The manifest lists ``tools_allowed`` (so an unlock adds to it, not to
    #: ``tools_opt_in``).
    declares_tools_allowed: bool = False
    tools_denied: tuple[str, ...] = ()
    can_spawn_agents: bool = False
    host_exec_socket: str = ""
    coding_repo_roots: str = ""
    #: ``None``: not checked here; the tool reports it when called.
    github_token: bool | None = None
    claude_cli: bool | None = None


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    available: bool
    tools: tuple[str, ...] = ()
    gate: str = ""
    gate_name: str = ""
    unlock: str = ""
    note: str = ""


@dataclass(frozen=True)
class _Spec:
    key: str
    label: str
    tools: tuple[str, ...]
    main_only: bool = False


#: Declared order is report order.
_SPECS: tuple[_Spec, ...] = (
    _Spec("exec", "run shell commands", ("exec",)),
    _Spec("host_exec", "run owner commands on the host, outside the engine", ("exec",), True),
    _Spec("execute_code", "run Python code", ("execute_code",)),
    _Spec("write_files", "create and change files", ("write_file", "edit_file")),
    _Spec(
        "git_pr",
        "commit, push and open pull requests",
        ("git_commit", "git_push", "create_pull_request"),
    ),
    _Spec("coding_jobs", "start Claude Code jobs that change a repository", ("claude_code_start",)),
    _Spec("browser", "drive a web browser", ("browser",)),
    _Spec("spawn", "start other agents", ("spawn_agent", "spawn_agents")),
    _Spec("vault_set", "store a credential in the vault", ("vault_set",)),
    _Spec("send_mail", "send and reply to email", (MAIL_SEND_TOOL, MAIL_REPLY_TOOL)),
)


def _grant_unlock(facts: Facts, tool: str) -> tuple[str, str]:
    """``(gate_name, unlock)`` for a tool this agent's manifest does not grant."""
    from fnmatch import fnmatch

    from robothor.engine.tools.constants import OPT_IN_TOOLS, SPAWN_TOOLS

    path = f"docs/agents/{facts.agent_id}.yaml"
    if any(fnmatch(tool, pattern) for pattern in facts.tools_denied):
        return "tools_denied", f"an operator removes '{tool}' from tools_denied in {path}"
    if tool in SPAWN_TOOLS and not facts.can_spawn_agents:
        return "can_spawn_agents", f"an operator sets can_spawn_agents: true in {path}"
    key = (
        "tools_opt_in"
        if tool in OPT_IN_TOOLS and not facts.declares_tools_allowed
        else ("tools_allowed")
    )
    return key, f"an operator adds '{tool}' to {key} in {path} and reloads the agent"


def manifest_unlock(agent_config: AgentConfig | None, tool_name: str) -> str:
    """The unlock sentence for a tool outside this agent's grant."""
    if agent_config is None:
        return f"an operator adds '{tool_name}' to the agent's tools_allowed"
    return _grant_unlock(_manifest_facts(agent_config), tool_name)[1]


def _runtime_gate(spec: _Spec, facts: Facts) -> tuple[str, str] | None:
    """``(gate_name, unlock)`` when the instance lacks what a granted capability needs."""
    if spec.key == "host_exec" and not facts.host_exec_socket:
        return (
            "ROBOTHOR_HOST_EXEC_SOCKET",
            "an operator installs the host-exec service and sets ROBOTHOR_HOST_EXEC_SOCKET; "
            "it then serves the verified owner's own top-level chat (plain exec still works)",
        )
    if spec.key == "git_pr" and facts.github_token is False:
        return (
            "GITHUB_TOKEN",
            "store a GitHub token as GITHUB_TOKEN with vault_set (the person can paste it "
            "in chat) and retry",
        )
    if spec.key == "coding_jobs":
        if not facts.coding_repo_roots:
            return (
                "ROBOTHOR_CODING_REPO_ROOTS",
                "an operator sets ROBOTHOR_CODING_REPO_ROOTS to the directories coding jobs "
                "may work under (config_set can write it)",
            )
        if facts.claude_cli is False:
            return (
                "claude CLI",
                "an operator installs the Claude Code CLI for the engine's user, or sets "
                "ROBOTHOR_CLAUDE_BIN, then runs `robothor claude-code login`",
            )
    return None


def assess(granted: Iterable[str], facts: Facts) -> list[Capability]:
    """Every capability, available or gated, in declared order. Pure."""
    names = frozenset(granted)
    out: list[Capability] = []
    for spec in _SPECS:
        if spec.main_only and facts.agent_id != "main":
            continue
        have = tuple(t for t in spec.tools if t in names)
        if not have:
            gate_name, unlock = _grant_unlock(facts, spec.tools[0])
            out.append(
                Capability(spec.key, spec.label, False, spec.tools, "allowlist", gate_name, unlock)
            )
            continue
        blocked = _runtime_gate(spec, facts)
        if blocked:
            out.append(
                Capability(spec.key, spec.label, False, have, "runtime", blocked[0], blocked[1])
            )
            continue
        note = ""
        if spec.key == "git_pr" and facts.github_token is None:
            note = "needs GITHUB_TOKEN when called; vault_set stores it"
        out.append(Capability(spec.key, spec.label, True, have, note=note))
    return out


def _manifest_facts(config: AgentConfig) -> Facts:
    return Facts(
        agent_id=config.id,
        service_role=config.service_role or "service",
        declares_tools_allowed=bool(config.tools_allowed),
        tools_denied=tuple(config.tools_denied or ()),
        can_spawn_agents=bool(config.can_spawn_agents),
    )


def facts_for(config: AgentConfig) -> Facts:
    """The session-stable facts: the manifest and the engine's settings only."""
    from robothor.settings import get_settings

    settings = get_settings()
    return replace(
        _manifest_facts(config),
        host_exec_socket=settings.engine.host_exec_socket or "",
        coding_repo_roots=settings.coding.repo_roots or "",
    )


def render(capabilities: list[Capability], facts: Facts) -> str:
    """The prompt block. Deterministic for the same input."""
    lines = [
        HEADER,
        "",
        "Built from your manifest and this instance's settings. It does not change "
        "during this conversation.",
        "",
        "Available:",
    ]
    available = [c for c in capabilities if c.available]
    gated = [c for c in capabilities if not c.available]
    for c in available:
        tools = ", ".join(f"`{t}`" for t in c.tools)
        note = f"; {c.note}" if c.note else ""
        lines.append(f"- {c.key}: {c.label} ({tools}){note}")
    if not available:
        lines.append("- none")
    lines += ["", "Gated (what refuses it, and what unlocks it):"]
    for c in gated:
        lines.append(f"- {c.key}: {c.label}. {c.gate} gate {c.gate_name}. Unlock: {c.unlock}.")
    if not gated:
        lines.append("- none")
    lines += [
        "",
        "Who you act for: in a chat you act with the role of the person you are talking "
        "to, and an owner or admin may use everything available above. Scheduled, hook, "
        f"workflow and sub-agent runs act as service role '{facts.service_role}'.",
        "",
        "Never say you cannot do something listed as available: do it. When a tool is "
        "refused, its result names the gate and the unlock; tell the person both and "
        "offer the next step, rather than saying you cannot or looking for a way round.",
    ]
    return "\n".join(lines)


def capability_block(config: AgentConfig) -> str:
    """The block for ``config``'s system prompt, or ``""`` with the flag off."""
    from robothor.engine.feature_flags import capability_block_enabled

    if not capability_block_enabled():
        return ""
    from robothor.engine.tools.registry import get_registry

    facts = facts_for(config)
    granted = get_registry().get_tool_names(config)
    return render(assess(granted, facts), facts)

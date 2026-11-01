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
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import MAIL_REPLY_TOOL, MAIL_SEND_TOOL

if TYPE_CHECKING:
    from collections.abc import Iterable

    from robothor.engine.models import AgentConfig

__all__ = [
    "Capability",
    "Facts",
    "assess",
    "capability_block",
    "coding_workspace",
    "credential_intake_mode",
    "facts_for",
    "github_token_unlock",
    "manifest_unlock",
    "render",
    "settings_facts",
    "vault_set_admitted",
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
    #: ``ROBOTHOR_CODING_WORKSPACE`` as the coding settings read it: an implicit
    #: repo root, so it alone admits a coding job. ``None``: this build's coding
    #: jobs have no workspace (see :func:`coding_workspace`).
    coding_workspace: str | None = None
    #: ``v2.credentials`` in the manifest (``operator`` admits vault_set).
    credential_tier: str = ""
    #: The vault handler's credential-intake mode (off | observe | enforce): at
    #: ``enforce``, vault_set is admitted in an owner's or admin's own chat with
    #: the default chat agent. ``None``: this build has no credential intake
    #: (see :func:`credential_intake_mode`).
    credential_intake: str | None = None
    #: ``ROBOTHOR_DEFAULT_CHAT_AGENT``: the agent credential intake serves.
    default_chat_agent: str = "main"
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
    denied_by = next((p for p in facts.tools_denied if fnmatch(tool, p)), None)
    if denied_by == tool:
        return "tools_denied", f"an operator removes '{tool}' from tools_denied in {path}"
    if denied_by is not None:
        # The manifest holds the pattern, not the tool: name what is there.
        return "tools_denied", (
            f"an operator removes or narrows the tools_denied entry '{denied_by}' (it "
            f"matches '{tool}') in {path}"
        )
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


def vault_set_admitted(facts: Facts) -> bool:
    """Whether ``vault_set`` gets past the credential gate for this agent.

    Mirrors the vault handler: the manifest's operator tier always admits it;
    otherwise credential intake, where this build has it, admits it at
    ``enforce`` in an owner's or admin's own chat with the default chat agent
    (the per-person half is checked when called).
    """
    if facts.credential_tier == "operator":
        return True
    return facts.credential_intake == "enforce" and facts.agent_id == facts.default_chat_agent


def _credential_gate(facts: Facts) -> tuple[str, str, str]:
    path = f"docs/agents/{facts.agent_id}.yaml"
    tier = f"adds `credentials: operator` under `v2:` in {path}"
    if facts.credential_intake is not None and facts.agent_id == facts.default_chat_agent:
        return (
            "flag",
            "ROBOTHOR_CREDENTIAL_INTAKE",
            f"an operator sets ROBOTHOR_CREDENTIAL_INTAKE=enforce (then an owner or admin "
            f"can hand a credential over in chat), or {tier}",
        )
    return "allowlist", "credential_tier", f"an operator {tier}"


def _runtime_gate(spec: _Spec, facts: Facts) -> tuple[str, str, str] | None:
    """``(gate, gate_name, unlock)`` when something besides the grant refuses it."""
    if spec.key == "vault_set" and not vault_set_admitted(facts):
        return _credential_gate(facts)
    if spec.key == "host_exec" and not facts.host_exec_socket:
        return (
            "runtime",
            "ROBOTHOR_HOST_EXEC_SOCKET",
            "an operator installs the host-exec service and sets ROBOTHOR_HOST_EXEC_SOCKET; "
            "it then serves the verified owner's own top-level chat (plain exec still works)",
        )
    if spec.key == "git_pr" and facts.github_token is False:
        return ("runtime", "GITHUB_TOKEN", github_token_unlock(vault_set_admitted(facts)))
    if spec.key == "coding_jobs":
        if not facts.coding_repo_roots and not facts.coding_workspace:
            roots = "ROBOTHOR_CODING_REPO_ROOTS (the directories they may work under)"
            if facts.coding_workspace is not None:
                roots = (
                    "ROBOTHOR_CODING_WORKSPACE (where coding jobs clone repositories) or " + roots
                )
            return ("runtime", "ROBOTHOR_CODING_REPO_ROOTS", f"an operator sets {roots}")
        if facts.claude_cli is False:
            return (
                "runtime",
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
            out.append(Capability(spec.key, spec.label, False, have, *blocked))
            continue
        note = ""
        if spec.key == "git_pr" and facts.github_token is None:
            note = "needs GITHUB_TOKEN when called; " + (
                "vault_set stores it" if vault_set_admitted(facts) else "an operator stores it"
            )
        if spec.key == "vault_set" and facts.credential_tier != "operator":
            note = "only in an owner or admin's own chat with you"
        out.append(Capability(spec.key, spec.label, True, have, note=note))
    return out


def _manifest_facts(config: AgentConfig) -> Facts:
    return Facts(
        agent_id=config.id,
        service_role=config.service_role or "service",
        declares_tools_allowed=bool(config.tools_allowed),
        tools_denied=tuple(config.tools_denied or ()),
        can_spawn_agents=bool(config.can_spawn_agents),
        credential_tier=str(getattr(config, "credential_tier", "") or "").strip().lower(),
    )


def github_token_unlock(vault_set_ok: bool) -> str:
    """The unlock for a missing ``GITHUB_TOKEN``: vault_set only where it is admitted."""
    if vault_set_ok:
        return (
            "store a GitHub token as GITHUB_TOKEN with vault_set (the person can paste it "
            "in chat) and retry; no restart is needed"
        )
    return (
        "an operator stores a GitHub token as GITHUB_TOKEN in the instance's vault and the "
        "call is retried; no restart is needed"
    )


def facts_for(config: AgentConfig) -> Facts:
    """The session-stable facts: the manifest and the engine's settings only."""
    from robothor.settings import get_settings

    return replace(_manifest_facts(config), **settings_facts(get_settings()))


def settings_facts(settings: Any) -> dict[str, Any]:
    """The :class:`Facts` fields read from the engine's settings and build."""
    return {
        "host_exec_socket": settings.engine.host_exec_socket or "",
        "coding_repo_roots": settings.coding.repo_roots or "",
        "coding_workspace": coding_workspace(settings),
        "credential_intake": credential_intake_mode(),
        "default_chat_agent": settings.engine.default_chat_agent or "main",
    }


def credential_intake_mode() -> str | None:
    """The credential-intake mode the vault handler reads, or ``None`` without one.

    Probed, not read from the environment by name: the vault handler admits
    intake only through ``feature_flags.credential_intake_mode``, so a build
    without that function has no credential intake whatever
    ``ROBOTHOR_CREDENTIAL_INTAKE`` says, and naming it as an unlock would send
    an operator to a setting that does nothing.
    """
    from robothor.engine import feature_flags

    probe = getattr(feature_flags, "credential_intake_mode", None)
    return str(probe()) if callable(probe) else None


def coding_workspace(settings: Any) -> str | None:
    """``ROBOTHOR_CODING_WORKSPACE`` as the coding jobs read it, or ``None`` without one.

    Probed on the settings model: coding jobs take the workspace as a repo root
    only where the coding settings declare a ``workspace`` field. Where they do
    not, the environment variable is read by nothing.
    """
    value = getattr(settings.coding, "workspace", None)
    return None if value is None else str(value).strip()


def render(capabilities: list[Capability], facts: Facts, *, relay_refusals: bool = False) -> str:
    """The prompt block. Deterministic for the same input.

    ``relay_refusals`` adds the sentence telling the model to relay a refusal's
    gate and unlock; pass it only when ``ROBOTHOR_REFUSAL_PAYLOAD_V2`` is on.
    """
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
        "Never say you cannot do something listed as available: do it.",
    ]
    if relay_refusals:
        # Only when the payload carries them: told to relay a gate the result
        # does not have, a model invents one (the reason rule 22 is gated too).
        lines[-1] += (
            " When a tool is refused, its result names the gate and the unlock; tell the "
            "person both and offer the next step, rather than saying you cannot or "
            "looking for a way round."
        )
    return "\n".join(lines)


def capability_block(config: AgentConfig) -> str:
    """The block for ``config``'s system prompt, or ``""`` with the flag off."""
    from robothor.engine.feature_flags import capability_block_enabled, refusal_payload_v2_enabled

    if not capability_block_enabled():
        return ""
    from robothor.engine.tools.registry import get_registry

    facts = facts_for(config)
    granted = get_registry().get_tool_names(config)
    return render(assess(granted, facts), facts, relay_refusals=refusal_payload_v2_enabled())

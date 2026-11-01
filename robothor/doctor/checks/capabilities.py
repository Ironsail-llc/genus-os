"""``capabilities.agent``: what each agent can do on this instance.

One row per ``<agent>:<capability>``. A capability the agent has passes; one it
lacks fails at ``info`` severity -- a lacking capability is a fact about the
instance, not a defect -- and names the gate (``allowlist``, ``runtime``) and
the unlock.

The verdict comes from :func:`robothor.engine.capabilities.assess`, the same
function that writes the agent's own "What you can do here" prompt block, so
the operator and the agent read one answer. The doctor adds the two facts the
prompt deliberately leaves out because they need I/O: whether a GitHub token
resolves, and whether the Claude Code CLI is installed.

``genus doctor --category capabilities --agent main`` reports one agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext
    from robothor.engine.capabilities import Facts

__all__ = ["CHECKS"]


def _io_facts() -> tuple[bool, bool]:
    """``(GITHUB_TOKEN resolves, Claude Code CLI found)``. Blocking."""
    from robothor.engine.coding import runner
    from robothor.secrets import secret_source

    token = secret_source("GITHUB_TOKEN") in ("vault", "env")
    try:
        runner.resolve_claude_binary()
        cli = True
    except runner.ClaudeCodeError:
        cli = False
    return token, cli


def _facts(manifest: dict[str, Any], token: bool, cli: bool) -> Facts:
    from robothor.engine.capabilities import Facts, env_facts
    from robothor.settings import get_settings

    settings = get_settings()
    v2 = manifest.get("v2")
    v2 = v2 if isinstance(v2, dict) else {}
    workspace, intake = env_facts()
    return Facts(
        agent_id=str(manifest.get("id")),
        service_role=str(manifest.get("service_role") or "service"),
        declares_tools_allowed=bool(manifest.get("tools_allowed")),
        tools_denied=tuple(str(t) for t in manifest.get("tools_denied") or ()),
        can_spawn_agents=bool(manifest.get("can_spawn_agents")),
        host_exec_socket=settings.engine.host_exec_socket or "",
        coding_repo_roots=settings.coding.repo_roots or "",
        github_token=token,
        claude_cli=cli,
        credential_tier=str(v2.get("credentials", "") or "").strip().lower(),
        coding_workspace=workspace,
        credential_intake=intake,
    )


async def _agent_capabilities(ctx: DoctorContext) -> list[Result]:
    """What each agent can do here; a gated row names the gate and the unlock.

    Not a verdict on the instance: an agent without a capability may be meant
    to be without it. When an agent says it cannot do something the person
    expected, read its rows here: an ``allowlist`` gate is a manifest change
    (``tools_allowed``, ``tools_opt_in``, ``tools_denied``), a ``runtime`` gate
    is a setting or credential the instance lacks (``ROBOTHOR_CODING_REPO_ROOTS``,
    ``ROBOTHOR_HOST_EXEC_SOCKET``, ``GITHUB_TOKEN``, the Claude Code CLI).
    """
    from robothor.doctor.checks.tools import _granted, _manifest_dir, _registered, _scan_manifests
    from robothor.engine.capabilities import assess

    directory = _manifest_dir(ctx)
    manifests = await ctx.run_blocking(_scan_manifests, directory)
    if ctx.agent:
        manifests = [m for m in manifests if str(m.get("id")) == ctx.agent]
        if not manifests:
            return [
                fail(
                    f"no agent '{ctx.agent}' among the manifests in {directory}",
                    sub_id=ctx.agent,
                )
            ]
    if not manifests:
        return [skip(f"no agent manifests in {directory}")]

    registered = await _registered(ctx)
    token, cli = await ctx.run_blocking(_io_facts)
    rows: list[Result] = []
    for manifest in manifests:
        facts = _facts(manifest, token, cli)
        for cap in assess(_granted(manifest, registered), facts):
            sub_id = f"{facts.agent_id}:{cap.key}"
            if cap.available:
                tools = ", ".join(cap.tools)
                note = f"; {cap.note}" if cap.note else ""
                rows.append(
                    Result(status="pass", detail=f"{cap.label} ({tools}){note}", sub_id=sub_id)
                )
            else:
                rows.append(
                    fail(
                        f"{cap.label}: {cap.gate} gate {cap.gate_name}. Unlock: {cap.unlock}",
                        sub_id=sub_id,
                    )
                )
    return rows or [ok("no capabilities to report")]


CHECKS: tuple[Check, ...] = (
    Check(
        id="capabilities.agent",
        title="What each agent can do here, and what unlocks the rest",
        category="capabilities",
        severity="info",
        run=_agent_capabilities,
    ),
)

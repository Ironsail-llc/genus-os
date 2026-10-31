"""Sandbox: is there a container runtime, and where would each agent's exec run?

Two readings, never a failure on their own. An instance with no podman is not
broken -- most agents never ask for a container -- but an operator deciding
whether to install one, or to turn on ROBOTHOR_SANDBOX_PLAYGROUND, needs to see
what each exec-holding agent would do for an owner and for a member.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, info, ok

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]


def _manifest_dir(ctx: DoctorContext) -> Path:
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().manifest_dir


async def _runtime(ctx: DoctorContext) -> Result:
    """Which container runtime sandboxed exec would use, and whether it answers.

    Install rootless podman for the engine's service user (or set
    ROBOTHOR_SANDBOX_BINARY, or select a plugin with ROBOTHOR_SANDBOX_BACKEND)
    to make it available.
    """
    from robothor.engine import sandbox

    status = await ctx.run_blocking(lambda: sandbox.runtime_status(refresh=True))
    if status.available:
        return ok(f"{status.binary} is available")
    return info(f"{status.binary} not available: {status.reason}")


async def _policy(ctx: DoctorContext) -> Result:
    """Per exec-holding agent: where its exec runs for an owner and for a member.

    With ROBOTHOR_SANDBOX_PLAYGROUND off, an agent that wants a container the
    engine cannot start has its runs failed; with it on, an owner or admin runs
    on the host (degraded) and anyone else is refused with the unlock named.
    """
    from robothor.doctor.checks.tools import _scan_manifests
    from robothor.engine import sandbox
    from robothor.engine.feature_flags import sandbox_default_mode, sandbox_playground_enabled
    from robothor.engine.sandbox_policy import agent_holds_exec, resolve_execution_plan

    status = await ctx.run_blocking(sandbox.runtime_status)
    manifests = await ctx.run_blocking(_scan_manifests, _manifest_dir(ctx))
    mode = sandbox_default_mode()
    playground = sandbox_playground_enabled()

    lines: list[str] = []
    for manifest in manifests:
        cfg = SimpleNamespace(
            id=str(manifest.get("id", "")),
            sandbox=str(manifest.get("sandbox", "") or ""),
            tools_allowed=list(manifest.get("tools_allowed") or []),
            tools_denied=list(manifest.get("tools_denied") or []),
        )
        if not agent_holds_exec(cfg):
            continue
        cells = []
        for role in ("owner", "member"):
            plan = resolve_execution_plan(
                cfg, mode, agent_id=cfg.id, user_role=role, runtime=status
            )
            if (
                not playground
                and plan.where != "sandbox"
                and (plan.degraded or plan.where == "refuse")
            ):
                cells.append(f"{role} run fails")
            else:
                cells.append(f"{role} {plan.where}" + (" (degraded)" if plan.degraded else ""))
        lines.append(f"{cfg.id}: {', '.join(cells)}")

    header = (
        f"playground {'on' if playground else 'off'}; sandbox mode {mode}; "
        f"runtime {'available' if status.available else 'not available'}"
    )
    if not lines:
        return ok(f"{header}; no agent holds exec")
    return ok("\n".join([*lines, header]))


CHECKS: tuple[Check, ...] = (
    Check(
        id="sandbox.runtime",
        title="A container runtime is available for sandboxed exec",
        category="sandbox",
        severity="recommended",
        run=_runtime,
    ),
    Check(
        id="sandbox.policy",
        title="Where each exec-holding agent's commands run, for an owner and a member",
        category="sandbox",
        severity="recommended",
        run=_policy,
    ),
)

"""claude_code_* tools — an agent delegates a coding task to Claude Code.

The work is in :mod:`robothor.engine.coding`; this module is the agent-facing
surface: argument checks, tenant scoping (every lookup is by job id AND the
caller's tenant), the benchmark refusal, and a status view small enough to fit
the persisted step record.

Gating, in the order a call meets it: the tools are in ``OPT_IN_TOOLS``, so
only an agent whose manifest names them is offered them (and the runner's
``tools_allowed`` guard refuses a call to one it was not offered); they are in
``EXTERNAL_SIDE_EFFECT_TOOLS``; and every handler refuses ``ctx.is_benchmark``
outright, because a job spends real money and writes real commits.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.coding.jobs import CodingJob
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

_DEFAULT_WAIT = 300
_MAX_WAIT = 1800
_RECENT_EVENTS = 8


def _benchmark_refusal(name: str) -> dict[str, Any]:
    return {
        "error": f"Tool '{name}' is disabled in benchmark mode: it runs a real, paid coding agent.",
        "guard": "is_benchmark",
    }


def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else "…" + value[-(limit - 1) :]


def _view(job: CodingJob) -> dict[str, Any]:
    """The job as the agent sees it — bounded, evidence first."""
    verify = job.result.get("verify") or {}
    view: dict[str, Any] = {
        "job_id": job.id,
        "status": str(job.status),
        "mode": job.mode,
        "rounds": job.rounds,
        "max_rounds": job.max_rounds,
        "cost_usd": round(job.cost_usd, 4),
        "turns": job.turns,
        "branch": job.branch,
        "worktree": job.worktree_path,
        "session_id": job.session_id,
    }
    if job.error:
        view["error"] = _clip(job.error, 500)
    if verify:
        view["verify"] = {
            "passed": verify.get("passed"),
            "exit_code": verify.get("exit_code"),
            "output_sha256": verify.get("output_sha256"),
            "reasons": verify.get("reasons") or [],
            "output_tail": _clip(verify.get("output_tail"), 900),
        }
    if job.result.get("commit_sha"):
        view["commit_sha"] = job.result["commit_sha"]
        view["commit_count"] = len(job.result.get("commits") or [])
    if job.evidence:
        view["evidence"] = [
            {
                "kind": e.get("kind"),
                "reference": e.get("reference"),
                "summary": _clip(e.get("summary"), 200),
            }
            for e in job.evidence
        ]
    if job.result.get("structured_output") is not None:
        view["structured_output"] = job.result["structured_output"]
    if job.result.get("claude_result"):
        view["claude_said"] = _clip(job.result["claude_result"], 500)
    if job.events_tail:
        view["recent"] = [_clip(e, 160) for e in job.events_tail[-_RECENT_EVENTS:]]
    if job.pending_followups:
        view["pending_followups"] = len(job.pending_followups)
    if str(job.status) in ("queued", "running"):
        view["next"] = "call claude_code_wait again"
    return view


def _check_repo(raw: str) -> str | None:
    """Why ``raw`` may not be worked on, or None."""
    if not raw:
        return "repo_path is required"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return "repo_path must be an absolute path"
    from robothor.settings import get_settings

    roots = [r for r in get_settings().coding.repo_roots.split(os.pathsep) if r.strip()]
    if roots:
        resolved = path.resolve()
        if not any(resolved.is_relative_to(Path(r).expanduser().resolve()) for r in roots):
            return f"repo_path is outside ROBOTHOR_CODING_REPO_ROOTS ({os.pathsep.join(roots)})"
    return None


async def _start(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal("claude_code_start")
    from robothor.engine.coding.jobs import Acceptance, get_manager
    from robothor.engine.coding.worktree import WorktreeError

    task = str(args.get("task") or "").strip()
    if not task:
        return {"error": "task is required: a precise spec of the change"}
    repo = str(args.get("repo_path") or "").strip()
    problem = _check_repo(repo)
    if problem:
        return {"error": problem}
    raw_acceptance = args.get("acceptance")
    if not isinstance(raw_acceptance, dict):
        return {"error": "acceptance is required: {verify_command, require_commit}"}
    mode = str(args.get("mode") or "code")
    if mode not in ("code", "review", "readonly"):
        return {"error": f"mode must be code, review or readonly, not {mode!r}"}
    try:
        budget = float(args["max_budget_usd"]) if args.get("max_budget_usd") is not None else None
        rounds = int(args.get("max_rounds") or 3)
    except (TypeError, ValueError):
        return {"error": "max_budget_usd must be a number and max_rounds an integer"}
    schema = args.get("json_schema")
    if schema is not None and not isinstance(schema, dict):
        return {"error": "json_schema must be an object"}

    try:
        job = await get_manager().start(
            tenant_id=ctx.tenant_id,
            agent_id=ctx.agent_id,
            task=task,
            repo_path=str(Path(repo).expanduser()),
            acceptance=Acceptance.from_dict(raw_acceptance),
            mode=mode,
            model=(str(args["model"]).strip() or None) if args.get("model") else None,
            max_budget_usd=budget,
            max_rounds=rounds,
            base_ref=str(args.get("base_ref") or "HEAD"),
            run_id=ctx.run_id,
            grant_github=bool(args.get("grant_github")),
            json_schema=schema,
        )
    except (ValueError, WorktreeError) as exc:
        return {"error": str(exc)}
    return {
        "job_id": job.id,
        "status": str(job.status),
        "branch": job.branch,
        "worktree": job.worktree_path,
        "max_rounds": job.max_rounds,
        "max_budget_usd": job.max_budget_usd,
        "next": "call claude_code_wait with this job_id",
    }


def _not_found(job_id: str) -> dict[str, Any]:
    return {"error": f"coding job {job_id!r} not found"}


async def _status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal("claude_code_status")
    from robothor.engine.coding.jobs import get_manager

    job_id = str(args.get("job_id") or "")
    job = await get_manager().get(job_id, ctx.tenant_id)
    return _view(job) if job else _not_found(job_id)


async def _wait(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal("claude_code_wait")
    from robothor.engine import run_pacing
    from robothor.engine.coding.jobs import get_manager

    job_id = str(args.get("job_id") or "")
    try:
        requested = int(args.get("timeout_s") or _DEFAULT_WAIT)
    except (TypeError, ValueError):
        return {"error": "timeout_s must be an integer"}
    requested = max(1, min(requested, _MAX_WAIT))
    # A wait may not outlive the run that is waiting: leave it time to report.
    timeout, note = run_pacing.clamp_tool_timeout(requested, run_id=ctx.run_id or "")
    job = await get_manager().wait(job_id, ctx.tenant_id, timeout_s=timeout)
    if job is None:
        return _not_found(job_id)
    view = _view(job)
    if note:
        view["timeout_note"] = note
    return view


async def _followup(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal("claude_code_followup")
    from robothor.engine.coding.jobs import get_manager
    from robothor.engine.coding.worktree import WorktreeError

    job_id = str(args.get("job_id") or "")
    message = str(args.get("message") or "").strip()
    if not message:
        return {"error": "message is required"}
    try:
        job = await get_manager().followup(job_id, ctx.tenant_id, message)
    except LookupError:
        return _not_found(job_id)
    except (ValueError, WorktreeError) as exc:
        return {"error": str(exc)}
    return _view(job)


async def _cancel(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal("claude_code_cancel")
    from robothor.engine.coding.jobs import get_manager

    job_id = str(args.get("job_id") or "")
    try:
        job = await get_manager().cancel(job_id, ctx.tenant_id)
    except LookupError:
        return _not_found(job_id)
    return _view(job)


HANDLERS["claude_code_start"] = _start
HANDLERS["claude_code_status"] = _status
HANDLERS["claude_code_wait"] = _wait
HANDLERS["claude_code_followup"] = _followup
HANDLERS["claude_code_cancel"] = _cancel

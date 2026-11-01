"""claude_code_* tools — an agent delegates a coding task to Claude Code.

The work is in :mod:`robothor.engine.coding`; this module is the agent-facing
surface: argument checks, scoping (every lookup is by job id AND the caller's
tenant AND the agent that started the job — only the owner role sees every
agent's jobs), the benchmark refusal, and a status view small enough to fit
the persisted step record.

Gating, in the order a call meets it: the tools are in ``OPT_IN_TOOLS``, so
only an agent whose manifest names them is offered them (and the runner's
``tools_allowed`` guard refuses a call to one it was not offered); they are in
``EXTERNAL_SIDE_EFFECT_TOOLS``; and every handler refuses ``ctx.is_benchmark``
outright, because a job spends real money and writes real commits.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
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


def _scope(ctx: ToolContext) -> str | None:
    """Whose jobs the caller may see: its own, or — for the owner — any in the tenant."""
    return None if ctx.user_role == "owner" else ctx.agent_id


async def _start(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal("claude_code_start")
    from robothor.engine.coding.jobs import Acceptance, get_manager
    from robothor.engine.coding.worktree import WorktreeError

    task = str(args.get("task") or "").strip()
    if not task:
        return {"error": "task is required: a precise spec of the change"}
    repo = str(args.get("repo_path") or "").strip()
    if not repo:
        return {"error": "repo_path is required"}
    if not Path(repo).expanduser().is_absolute():
        return {"error": "repo_path must be an absolute path"}
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
    from robothor.engine.coding.runner import EFFORTS

    effort = str(args.get("effort") or "").strip() or None
    if effort is not None and effort not in EFFORTS:
        return {"error": f"effort must be one of {', '.join(EFFORTS)}, not {effort!r}"}
    try:
        max_turns = int(args["max_turns"]) if args.get("max_turns") is not None else None
        if max_turns is not None and not 1 <= max_turns <= 500:
            raise ValueError
    except (TypeError, ValueError):
        return {"error": "max_turns must be an integer from 1 to 500"}
    try:
        round_timeout = (
            float(args["round_timeout_s"]) if args.get("round_timeout_s") is not None else None
        )
        if round_timeout is not None and not 60 <= round_timeout <= 7200:
            raise ValueError
    except (TypeError, ValueError):
        return {"error": "round_timeout_s must be seconds from 60 to 7200"}

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
            effort=effort,
            max_turns=max_turns,
            round_timeout_s=round_timeout,
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
    job = await get_manager().get(job_id, ctx.tenant_id, agent_id=_scope(ctx))
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
    job = await get_manager().wait(job_id, ctx.tenant_id, timeout_s=timeout, agent_id=_scope(ctx))
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
        job = await get_manager().followup(job_id, ctx.tenant_id, message, agent_id=_scope(ctx))
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
        job = await get_manager().cancel(job_id, ctx.tenant_id, agent_id=_scope(ctx))
    except LookupError:
        return _not_found(job_id)
    return _view(job)


# ── putting a repository on disk, and publishing a verified job ──────

#: Roles that may open a pull request from chat. Unattended runs dispatch as
#: ``service`` and never publish (migration 165 denies it at the RBAC gate too).
_PUBLISH_ROLES = frozenset({"owner", "admin", "super_admin"})
_TICKET = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


async def _github_token(tenant_id: str) -> str:
    from robothor.engine.coding import repos

    with contextlib.suppress(Exception):
        return await asyncio.to_thread(repos.github_token, tenant_id) or ""
    return ""


async def _audit_publish(ctx: ToolContext, status: str, details: dict[str, Any]) -> None:
    from robothor.audit.logger import log_event

    await asyncio.to_thread(
        log_event,
        "coding.publish",
        "claude_code_publish",
        category="agent",
        actor=ctx.agent_id or "unknown",
        user_id=ctx.user_id,
        details={"tenant_id": ctx.tenant_id, "run_id": ctx.run_id, "role": ctx.user_role} | details,
        status=status,
    )


async def _prepare(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal("claude_code_repo_prepare")
    from robothor.engine.coding import repos

    repo = str(args.get("repo") or "").strip()
    try:
        repos.workspace_dir()
        repos.allowed_remote(repo)
        prepared = await repos.ensure_repo(repo, token=await _github_token(ctx.tenant_id))
    except repos.RepoError as exc:
        return {"error": str(exc)}
    return {
        "repo_path": str(prepared.path),
        "remote": prepared.remote.slug,
        "default_branch": prepared.default_branch,
        "head_sha": prepared.head_sha,
        "cloned": prepared.cloned,
        "next": "claude_code_start with this repo_path; then claude_code_publish once it is done",
    }


def _unpublishable(job: CodingJob) -> str:
    """Why ``job`` may not be published; empty when it may."""
    verify = job.result.get("verify") or {}
    if str(job.status) != "done" or not verify.get("passed") or not job.result.get("commit_sha"):
        return (
            f"coding job {job.id!r} is {job.status}; only a done job whose verify passed "
            "and that made a commit can be published"
        )
    if job.mode != "code" or not job.branch:
        return f"a {job.mode} job has no branch to publish"
    return ""


async def _publish(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _benchmark_refusal("claude_code_publish")
    from robothor.engine.coding import publish, repos
    from robothor.engine.coding.jobs import get_manager
    from robothor.settings import get_settings

    job_id = str(args.get("job_id") or "")

    async def deny(reason: str) -> dict[str, Any]:
        """Every refusal is audited, with the sentence the agent is given."""
        await _audit_publish(ctx, "denied", {"job_id": job_id, "reason": reason[:300]})
        return {"error": reason}

    if ctx.user_role not in _PUBLISH_ROLES:
        return await deny(
            "claude_code_publish needs the owner or admin role; this caller's role is "
            f"{ctx.user_role or 'no role'}"
        )
    if not get_settings().coding.pr_enabled:
        return await deny("pull requests are off until ROBOTHOR_CODING_PR_ENABLED=true")
    title = str(args.get("title") or "").strip()
    if not title:
        return await deny("title is required")
    ticket = str(args.get("ticket") or "").strip()
    if ticket:
        if not _TICKET.match(ticket):
            return await deny(f"ticket must look like ABC-123, not {ticket!r}")
        if ticket not in title:
            title = f"{title} ({ticket})"
    job = await get_manager().get(job_id, ctx.tenant_id, agent_id=_scope(ctx))
    if job is None:
        return await deny(_not_found(job_id)["error"])
    refusal = _unpublishable(job)
    if refusal:
        return await deny(refusal)
    branch = str(job.branch)  # _unpublishable refused a job without one
    commit = str(job.result["commit_sha"])
    repo = Path(job.repo_path)
    try:
        remote = await repos.origin_remote(repo)
    except repos.RepoError as exc:
        return await deny(str(exc))
    if remote.host and remote.host != repos.GITHUB_HOST:
        return await deny(
            f"claude_code_publish opens pull requests on GitHub only; {remote.slug}'s "
            f"origin is on {remote.host}"
        )
    token = await _github_token(ctx.tenant_id)
    if not token:
        return await deny(
            "GITHUB_TOKEN not configured: store a token with contents and "
            "pull-request write access in the vault"
        )
    base = str(args.get("base") or "").strip() or await repos.default_branch(repo)
    details = {"job_id": job_id, "remote": remote.slug, "branch": branch, "base": base}
    try:
        await publish.push_branch(repo, branch, commit, remote, token)
        pr = await publish.open_draft_pr(
            remote,
            head=branch,
            base=base,
            title=title,
            body=str(args.get("body") or ""),
            token=token,
        )
    except publish.PublishError as exc:
        await _audit_publish(ctx, "error", details | {"reason": str(exc)[:300]})
        return {"error": str(exc)}
    await _audit_publish(ctx, "ok", details | {"pr_url": pr.url})
    return {
        "pr_url": pr.url,
        "pr_number": pr.number,
        "draft": pr.draft,
        "branch": branch,
        "base": base,
        "commit_sha": commit,
        "remote": remote.slug,
    }


HANDLERS["claude_code_start"] = _start
HANDLERS["claude_code_status"] = _status
HANDLERS["claude_code_wait"] = _wait
HANDLERS["claude_code_followup"] = _followup
HANDLERS["claude_code_cancel"] = _cancel
HANDLERS["claude_code_repo_prepare"] = _prepare
HANDLERS["claude_code_publish"] = _publish

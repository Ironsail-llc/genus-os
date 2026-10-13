"""pr_review_* tools — the deterministic halves of the pr-reviewer suite.

* ``pr_review_intake`` — poll GitHub and the Chat space, file one CRM task per
  pull-request head. Run from a cron workflow; no model involved; one run per
  tenant at a time. ``count_only`` just reads the queue (the run workflow).
  ``pr`` (a URL or ``owner/repo#N``) queues that one pull request on demand —
  how the operator asks main for a review — and reports a finished one.
* ``pr_review_prepare`` — fetch the head into a local clone, build the review
  prompt, START the read-only Claude Code review job and bind its id to the
  pull request. The agent never sees or edits the job's arguments.
* ``pr_review_finalize`` — for the job prepare bound, and only that job, read
  its structured output from the coding job itself, recompute the verdict in
  code, post the review (once), reply on our previous threads, resolve our
  own threads on approval, announce in Chat, record the state. The agent
  never handles the verdict, so it cannot post one unchecked. It posts
  through the GitHub review handlers directly, so the agent needs none of the
  ``github_*`` review-write tools.
* ``pr_review_work`` — the review worker the pr-review-run workflow calls:
  drains the open review tasks, ``ROBOTHOR_PR_REVIEW_WORKERS`` at once, each
  through prepare -> wait -> finalize -> resolve, with no model. Review jobs
  start as the reviewer agent (``ROBOTHOR_PR_REVIEW_AGENT``), whose manifest
  names GITHUB_TOKEN — never as the workflow, which names nothing. Finalize's
  digest lines go to the operator's Telegram as they arrive.
* ``pr_review_status`` — read-only report: what is in flight and queued, the
  window's verdicts, latency, failures and problems, or one pull request.

All five are opt-in (``OPT_IN_TOOLS``) and refuse a benchmark run. The logic
lives in :mod:`robothor.pr_review`.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.pr_review.clients import ChatClient, GitHubPort
    from robothor.pr_review.store import PrReviewStore
    from robothor.pr_review.tasks import TaskSink
    from robothor.pr_review.worker import TaskQueue

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: Tenants with a pr_review_work run in this process: one at a time each.
_WORKING: set[str] = set()

SKILL_NAME = "pr-review"


def _refusal(name: str) -> dict[str, Any]:
    return {
        "error": f"Tool '{name}' is disabled in benchmark mode: it reads and writes real "
        "pull requests and chat spaces.",
        "guard": "is_benchmark",
    }


# Factories: one place each outside system is built, so tests swap them.


def _store() -> PrReviewStore:
    from robothor.pr_review.store import PgStore

    return PgStore()


def _github() -> GitHubPort | None:
    from robothor.pr_review.clients import GitHubClient, GitHubError

    try:
        return GitHubClient()
    except GitHubError:
        return None


def _chat() -> ChatClient:
    from robothor.pr_review.clients import GwsChat

    return GwsChat()


def _tasks(tenant_id: str) -> TaskSink:
    from robothor.pr_review.tasks import CrmTaskSink

    return CrmTaskSink(tenant_id)


def _task_queue(tenant_id: str, agent_id: str) -> TaskQueue:
    from robothor.pr_review.tasks import CrmTaskQueue

    return CrmTaskQueue(tenant_id, agent_id)


def _review_tasks(tenant_id: str, agent_id: str) -> Any:
    """Every review task, open or resolved, newest first (for pr_review_status)."""

    async def lister() -> list[dict[str, Any]]:
        import asyncio

        from robothor.crm.dal import list_tasks
        from robothor.pr_review.tasks import TASK_TAG

        found: list[dict[str, Any]] = await asyncio.to_thread(
            list_tasks,
            assigned_to_agent=agent_id,
            tags=[TASK_TAG],
            exclude_resolved=False,
            include_snoozed=True,
            limit=2000,
            tenant_id=tenant_id,
        )
        return found

    return lister


def _telegram_notify() -> Any:
    """Send one line to the operator's Telegram, or None when there is no sender."""
    from robothor.engine.delivery import get_telegram_sender

    sender = get_telegram_sender()
    if sender is None:
        return None
    from robothor.engine.config import EngineConfig

    chat_id = EngineConfig.from_env().default_chat_id
    if not chat_id:
        return None

    async def notify(line: str) -> None:
        if not await sender(chat_id, line):
            logger.warning("pr_review digest: Telegram accepted none of %r", line[:80])

    return notify


def _job_state(tenant_id: str) -> Any:
    """``job_id -> (status, finished_at)`` for the intake's orphaned-review check."""

    async def lookup(job_id: str) -> tuple[str, datetime | None] | None:
        from robothor.engine.coding.jobs import get_manager

        job = await get_manager().get(job_id, tenant_id)
        if job is None:
            return None
        finished: datetime | None = None
        if job.finished_at:
            finished = datetime.fromisoformat(str(job.finished_at))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=UTC)
        return str(job.status), finished

    return lookup


def _skill_text() -> str | None:
    from robothor.engine.skills import get_skill_content

    return get_skill_content(SKILL_NAME)


def _ticket_fetcher(ctx: ToolContext) -> Any:
    """Fetch a ticket through the Jira handler (no model); None when Jira is not configured."""

    async def fetch(key: str) -> dict[str, Any] | None:
        from robothor.engine.tools.handlers import jira

        if not jira._get_base_url() or not jira._get_auth_header():
            return None
        result: dict[str, Any] = await jira._jira_get_issue(
            {"issue_key": key, "include_text": True}, ctx
        )
        return result

    return fetch


#: JQL words that would change the query's meaning inside ``text ~``.
_JQL_RESERVED = frozenset({"and", "or", "not", "empty", "null", "order", "by", "in", "is"})


def _ticket_searcher(ctx: ToolContext) -> Any:
    """Search the ticket system for issues matching a PR title; [] when Jira is not set up.

    The title becomes a handful of plain words — no quotes, operators or
    reserved words reach the JQL — searched within the repository's project.
    """

    async def search(prefix: str, title: str) -> list[dict[str, Any]]:
        from robothor.engine.tools.handlers import jira

        if not jira._get_base_url() or not jira._get_auth_header():
            return []
        words = [
            w
            for w in re.findall(r"[A-Za-z0-9]+", title.lower())
            if len(w) >= 3 and w not in _JQL_RESERVED
        ]
        project = re.sub(r"[^A-Za-z0-9_]", "", prefix)
        if not words or not project:
            return []
        jql = (
            f'project = "{project}" AND text ~ "{" ".join(dict.fromkeys(words[:8]))}" '
            "ORDER BY updated DESC"
        )
        result: dict[str, Any] = await jira._jira_search({"jql": jql, "max_results": 5}, ctx)
        return [
            {
                "key": str(i.get("key") or ""),
                "summary": str(i.get("summary") or ""),
                "status": str(i.get("status") or ""),
            }
            for i in result.get("issues") or []
            if isinstance(i, dict) and i.get("key")
        ]

    return search


def _pr_args(args: dict[str, Any]) -> tuple[str, int] | dict[str, Any]:
    from robothor.engine.tools.handlers.github_api import _pr_args as parse

    return parse(args)


class HandlerPoster:
    """Posts through the GitHub review tools' own handlers — one posting path."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def create_review(self, args: dict[str, Any]) -> dict[str, Any]:
        from robothor.engine.tools.handlers.github_api import _github_create_review

        result: dict[str, Any] = await _github_create_review(args, self.ctx)
        return result

    async def reply(self, repo: str, number: int, comment_id: int, body: str) -> dict[str, Any]:
        from robothor.engine.tools.handlers.github_api import _github_reply_review_comment

        result: dict[str, Any] = await _github_reply_review_comment(
            {"repo": repo, "number": number, "comment_id": comment_id, "body": body}, self.ctx
        )
        return result

    async def resolve(self, repo: str, number: int, review_ids: list[int]) -> dict[str, Any]:
        from robothor.engine.tools.handlers.github_api import _github_resolve_threads

        result: dict[str, Any] = await _github_resolve_threads(
            {"repo": repo, "number": number, "review_ids": review_ids}, self.ctx
        )
        return result


async def _intake(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _refusal("pr_review_intake")
    from robothor.pr_review.config import load_config
    from robothor.pr_review.intake import Intake

    cfg = load_config()
    if not cfg.configured:
        return {
            "configured": False,
            "open_tasks": 0,
            "queued_tasks": 0,
            "note": "set ROBOTHOR_PR_REVIEW_REPOS, _BOT_LOGIN or _CHAT_SPACE to enable",
        }
    poll = args.get("poll", True) is not False
    count_only = args.get("count_only") is True
    requested = str(args.get("pr") or "").strip()
    action = str(args.get("action") or "review").strip().lower()
    if action not in ("review", "skip"):
        return {"error": f"action must be 'review' or 'skip', not {action!r}"}
    if action == "skip" and not requested:
        return {"error": "action='skip' needs pr=<url or owner/repo#N>"}
    intake = Intake(
        cfg,
        _store(),
        ctx.tenant_id,
        tasks=_tasks(ctx.tenant_id),
        github=_github(),
        chat=_chat() if cfg.chat_space else None,
        job_state=_job_state(ctx.tenant_id),
    )
    if action == "skip":
        return await intake.skip(requested)
    if requested:
        return await intake.request(requested)
    return await intake.run(poll=poll, count_only=count_only)


async def _prepare(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _refusal("pr_review_prepare")
    from robothor.engine.tools.handlers.github_api import _get_token
    from robothor.pr_review.checkout import CheckoutError
    from robothor.pr_review.config import load_config
    from robothor.pr_review.review import prepare

    parsed = _pr_args(args)
    if isinstance(parsed, dict):
        return parsed
    repo, number = parsed
    skill = _skill_text()
    if not skill:
        return {"error": f"the {SKILL_NAME} skill is not installed"}
    github = _github()
    if github is None:
        return {"error": "GITHUB_TOKEN not configured"}

    async def start_job(start_args: dict[str, Any]) -> dict[str, Any]:
        from robothor.engine.tools.handlers.claude_code import _start

        result: dict[str, Any] = await _start(start_args, ctx)
        return result

    try:
        return await prepare(
            load_config(),
            _store(),
            ctx.tenant_id,
            repo,
            number,
            github=github,
            skill_text=skill,
            token=_get_token(),
            start_job=start_job,
            fetch_ticket=_ticket_fetcher(ctx),
            search_tickets=_ticket_searcher(ctx),
            job_status=_job_state(ctx.tenant_id),
        )
    except CheckoutError as exc:
        return {"error": f"checkout failed: {exc}"}


async def _finalize(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _refusal("pr_review_finalize")
    from robothor.pr_review.config import load_config
    from robothor.pr_review.review import finalize

    parsed = _pr_args(args)
    if isinstance(parsed, dict):
        return parsed
    repo, number = parsed
    dismiss = bool(args.get("dismiss"))
    job = None
    if not dismiss:
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            return {"error": "job_id is required (the claude_code_start job), or dismiss=true"}
        from robothor.engine.coding.jobs import get_manager

        job = await get_manager().get(job_id, ctx.tenant_id)
        if job is None:
            return {"error": f"coding job {job_id!r} not found"}
        if job.mode != "review":
            return {"error": "only a mode=review coding job can be finalized as a review"}
    github = _github()
    if github is None:
        return {"error": "GITHUB_TOKEN not configured"}
    cfg = load_config()
    return await finalize(
        cfg,
        _store(),
        ctx.tenant_id,
        repo,
        number,
        job=job,
        github=github,
        poster=HandlerPoster(ctx),
        chat=_chat() if cfg.chat_space else None,
        dismiss=dismiss,
    )


async def _work(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _refusal("pr_review_work")
    from robothor.pr_review.config import load_config
    from robothor.pr_review.worker import work

    cfg = load_config()
    if not cfg.configured:
        return {"configured": False, "processed": 0}
    if ctx.tenant_id in _WORKING:
        return {"skipped": "running", "processed": 0}
    # The jobs start as the reviewer agent: grant_github hands Claude Code the
    # token only for an agent whose own manifest names it, and the workflow's
    # own id ("workflow:<id>") names nothing.
    review_ctx = dataclasses.replace(ctx, agent_id=cfg.agent_id)

    async def prepare(repo: str, number: int) -> dict[str, Any]:
        return await _prepare({"repo": repo, "number": number}, review_ctx)

    async def wait(job_id: str, timeout_s: float) -> str:
        from robothor.engine.coding.jobs import get_manager

        job = await get_manager().wait(job_id, ctx.tenant_id, timeout_s=timeout_s)
        return str(job.status) if job is not None else ""

    async def finalize(repo: str, number: int, job_id: str) -> dict[str, Any]:
        return await _finalize({"repo": repo, "number": number, "job_id": job_id}, review_ctx)

    _WORKING.add(ctx.tenant_id)
    try:
        return await work(
            cfg,
            _task_queue(ctx.tenant_id, cfg.agent_id),
            prepare=prepare,
            wait=wait,
            finalize=finalize,
            notify=_telegram_notify(),
        )
    finally:
        _WORKING.discard(ctx.tenant_id)


async def _status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.is_benchmark:
        return _refusal("pr_review_status")
    from robothor.pr_review.config import load_config
    from robothor.pr_review.report import status_report

    cfg = load_config()
    if not cfg.configured:
        return {"configured": False, "note": "the pull-request reviewer is not set up"}
    try:
        hours = float(args.get("since_hours") or 24)
        if not 0 < hours <= 24 * 31:
            raise ValueError
    except (TypeError, ValueError):
        return {"error": "since_hours must be a number of hours from 1 to 744"}

    async def job_lookup(job_id: str) -> tuple[str, datetime | None] | None:
        from robothor.engine.coding.jobs import get_manager

        job = await get_manager().get(job_id, ctx.tenant_id)
        if job is None:
            return None
        started = datetime.fromisoformat(str(job.created_at)) if job.created_at else None
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return str(job.status), started

    now = datetime.now(UTC)
    return await status_report(
        cfg,
        _store(),
        ctx.tenant_id,
        list_tasks=_review_tasks(ctx.tenant_id, cfg.agent_id),
        job_lookup=job_lookup,
        now=now,
        since=now - timedelta(hours=hours),
        pr=str(args.get("pr") or "").strip(),
    )


HANDLERS["pr_review_intake"] = _intake
HANDLERS["pr_review_prepare"] = _prepare
HANDLERS["pr_review_finalize"] = _finalize
HANDLERS["pr_review_work"] = _work
HANDLERS["pr_review_status"] = _status

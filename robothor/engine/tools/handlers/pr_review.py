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

All three are opt-in (``OPT_IN_TOOLS``), are external side effects, and
refuse a benchmark run. The logic lives in :mod:`robothor.pr_review`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.pr_review.clients import ChatClient, GitHubPort
    from robothor.pr_review.store import PrReviewStore
    from robothor.pr_review.tasks import TaskSink

HANDLERS: dict[str, Any] = {}

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
    intake = Intake(
        cfg,
        _store(),
        ctx.tenant_id,
        tasks=_tasks(ctx.tenant_id),
        github=_github(),
        chat=_chat() if cfg.chat_space else None,
    )
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


HANDLERS["pr_review_intake"] = _intake
HANDLERS["pr_review_prepare"] = _prepare
HANDLERS["pr_review_finalize"] = _finalize

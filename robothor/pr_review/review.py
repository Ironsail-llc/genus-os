"""Prepare a review job, and finalize its result — the two halves around Claude Code.

:func:`prepare` turns a tracked pull request into the exact arguments for
``claude_code_start``: a clone fetched to the head, the review prompt (skill +
repository rules + this PR), the output schema, and the budget. The agent
passes them through unchanged.

:func:`finalize` is the only way a review is posted. It reads the job's
structured output straight from the coding job (the agent never relays it),
validates it, recomputes the verdict with :mod:`robothor.pr_review.policy`,
posts it, replies on our previous threads, resolves our own threads on
approval, announces in the Chat thread, and records the state. The agent
cannot change a verdict because it never handles one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol

from robothor.pr_review.checkout import ensure_checkout, read_at
from robothor.pr_review.policy import decide_verdict, extract_ticket_key
from robothor.pr_review.posting import decide_review
from robothor.pr_review.prompt import APPENDIX_PATHS, ReviewContext, build_review_prompt
from robothor.pr_review.schema import review_output_schema, validate_review_output
from robothor.pr_review.store import ACTIVE_STATUSES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.pr_review.clients import ChatClient, GitHubPort
    from robothor.pr_review.config import ReviewerConfig
    from robothor.pr_review.store import PrReviewRow, PrReviewStore

logger = logging.getLogger(__name__)

__all__ = ["Poster", "finalize", "prepare"]

_STATUS_FOR_VERDICT = {
    "APPROVE": "approved",
    "REQUEST_CHANGES": "changes_requested",
    "COMMENT": "commented",
}
_PRIOR_LABEL = {
    "resolved": "Resolved",
    "partially_resolved": "Partially resolved",
    "unresolved": "Still open",
}
_ERROR_MAX = 500


class Poster(Protocol):
    async def create_review(self, args: dict[str, Any]) -> dict[str, Any]: ...
    async def reply(self, repo: str, number: int, comment_id: int, body: str) -> dict[str, Any]: ...
    async def resolve(self, repo: str, number: int, review_ids: list[int]) -> dict[str, Any]: ...


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _pr_text(pr: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(pr.get("title") or ""),
        str((pr.get("head") or {}).get("ref") or ""),
        str(pr.get("body") or ""),
    )


async def _ticket_key(
    cfg: ReviewerConfig, github: GitHubPort, row: PrReviewRow, pr: dict[str, Any]
) -> str | None:
    title, branch, body = _pr_text(pr)
    key = extract_ticket_key(cfg.ticket_prefixes, title=title, branch=branch, body=body)
    if key or not cfg.require_ticket:
        return key
    commits = await github.list_commits(row.repo, row.number)
    messages = [str((c.get("commit") or {}).get("message") or "") for c in commits]
    return extract_ticket_key(cfg.ticket_prefixes, commit_messages=messages)


# ── prepare ─────────────────────────────────────────────────────────────


async def prepare(
    cfg: ReviewerConfig,
    store: PrReviewStore,
    tenant_id: str,
    repo: str,
    number: int,
    *,
    github: GitHubPort,
    skill_text: str,
    token: str,
    checkout: Callable[..., Awaitable[Any]] = ensure_checkout,
    reader: Callable[..., Awaitable[str | None]] = read_at,
    remote_url: str | None = None,
) -> dict[str, Any]:
    row = await store.get(tenant_id, repo, number)
    if row is None:
        return {"error": f"{repo}#{number} is not tracked; the intake files review tasks"}
    pr = await github.get_pr(row.repo, number)
    if pr is None:
        return {"error": f"{row.repo}#{number} not found on GitHub"}
    if str(pr.get("state") or "") != "open":
        row.status = "closed"
        row.pending_trigger = ""
        await store.save(row)
        return {"skip": True, "reason": "pull request is closed; resolve the task"}
    head = str((pr.get("head") or {}).get("sha") or "")
    base_ref = str((pr.get("base") or {}).get("ref") or "main")

    kind: Literal["initial", "rereview"] = "rereview" if row.last_reviewed_sha else "initial"
    compare_status = ""
    if kind == "rereview" and row.last_reviewed_sha != head:
        compare_status = await github.compare_status(row.repo, row.last_reviewed_sha, head)
    decision = decide_review(
        kind=kind,
        head_sha=head,
        last_reviewed_sha=row.last_reviewed_sha or None,
        compare_status=compare_status or None,
    )
    if decision.action == "skip":
        if row.status in ACTIVE_STATUSES:
            row.status = _STATUS_FOR_VERDICT.get(
                str(row.last_review.get("verdict") or ""), "commented"
            )
            await store.save(row)
        return {"skip": True, "reason": f"{decision.reason}: resolve the task"}

    path = await checkout(
        cfg.clone_dir(row.repo),
        number=number,
        head_sha=head,
        base_branch=base_ref,
        remote_url=remote_url or f"https://github.com/{row.repo}.git",
        token=token,
    )
    appendix, appendix_path = None, ""
    for candidate in APPENDIX_PATHS:
        appendix = await reader(path, head, candidate)
        if appendix:
            appendix_path = candidate
            break

    title, head_ref, _ = _pr_text(pr)
    previous = [
        {k: i.get(k) for k in ("comment_id", "path", "line", "severity", "title", "body")}
        for i in row.last_review.get("issues") or []
    ]
    ctx = ReviewContext(
        repo=row.repo,
        number=number,
        url=str(pr.get("html_url") or row.url),
        title=title,
        author=str((pr.get("user") or {}).get("login") or ""),
        head_ref=head_ref,
        base_ref=base_ref,
        head_sha=head,
        mode=decision.mode or "full",
        depth=row.depth or "full",
        since_sha=decision.since_sha or row.last_reviewed_sha,
        compare_status=compare_status,
        ticket_key=await _ticket_key(cfg, github, row, pr),
        appendix=appendix,
        appendix_path=appendix_path,
        previous_verdict=str(row.last_review.get("verdict") or ""),
        previous_summary=str(row.last_review.get("summary") or ""),
        previous_issues=previous,
    )
    row.status = "reviewing"
    row.head_sha = head
    row.queued_sha = head
    row.mode = ctx.mode
    await store.save(row)

    start_args: dict[str, Any] = {
        "task": build_review_prompt(skill_text, ctx),
        "repo_path": str(path),
        "acceptance": {"require_commit": False},
        "mode": "review",
        "base_ref": head,
        "json_schema": review_output_schema(),
        "grant_github": True,
        "max_budget_usd": cfg.review_budget_usd,
        "max_rounds": 2,
    }
    if cfg.review_model:
        start_args["model"] = cfg.review_model
    return {
        "repo": row.repo,
        "number": number,
        "head_sha": head,
        "mode": ctx.mode,
        "since_sha": ctx.since_sha if ctx.mode == "incremental" else "",
        "depth": ctx.depth,
        "start_args": start_args,
        "next": "call claude_code_start with start_args exactly as given, then claude_code_wait",
    }


# ── finalize ────────────────────────────────────────────────────────────


def _chat_line(row: PrReviewRow, text: str) -> str:
    return (
        f"<{row.url or f'https://github.com/{row.repo}/pull/{row.number}'}|#{row.number}>: {text}"
    )


async def _announce(
    store: PrReviewStore, chat: ChatClient | None, row: PrReviewRow, text: str
) -> bool:
    if not (chat and row.chat_space and row.chat_thread):
        return False
    try:
        name = await chat.reply(row.chat_space, row.chat_thread, _chat_line(row, text))
    except Exception as exc:  # noqa: BLE001 - the review is posted; the announcement is extra
        logger.warning("pr_review chat announce failed for %s: %s", row.key, exc)
        return False
    if name:
        await store.record_message(row.tenant_id, name, "self", "finalize announce")
    return True


async def _fail(
    store: PrReviewStore, chat: ChatClient | None, row: PrReviewRow, reason: str
) -> dict[str, Any]:
    row.status = "failed"
    row.error = reason[:_ERROR_MAX]
    row.attempts += 1
    await store.save(row)
    await _announce(
        store, chat, row, f'Automated review failed: {reason[:300]}. Reply "re-review" to retry.'
    )
    return {"status": "failed", "error": reason, "next": "resolve the task with this error"}


def _verdict_text(verdict: str, blocking: int) -> str:
    if verdict == "APPROVE":
        return "Approved"
    if blocking:
        return f"Needs changes — {_plural(blocking, 'blocking finding')}"
    return "Comments, nothing blocking"


async def finalize(
    cfg: ReviewerConfig,
    store: PrReviewStore,
    tenant_id: str,
    repo: str,
    number: int,
    *,
    job: Any | None,
    github: GitHubPort,
    poster: Poster,
    chat: ChatClient | None,
    dismiss: bool = False,
) -> dict[str, Any]:
    row = await store.get(tenant_id, repo, number)
    if row is None:
        return {"error": f"{repo}#{number} is not tracked"}

    if dismiss:
        row.status = (
            _STATUS_FOR_VERDICT.get(str(row.last_review.get("verdict") or ""), "commented")
            if row.last_reviewed_sha
            else "pending"
        )
        await store.save(row)
        return {"status": "dismissed", "next": "resolve the task: not a re-review request"}

    if job is None:
        return {"error": "coding job not found for this tenant"}
    status = str(getattr(job, "status", ""))
    if status in ("queued", "running"):
        return {"error": "the review job is still running; call claude_code_wait again"}
    if status != "done":
        return await _fail(store, chat, row, f"review job {status}: {getattr(job, 'error', '')}")

    output = (getattr(job, "result", None) or {}).get("structured_output")
    problems = validate_review_output(output)
    if problems:
        return await _fail(store, chat, row, "invalid review output: " + "; ".join(problems[:5]))
    assert isinstance(output, dict)
    reviewed_sha = str(getattr(job, "base_sha", "") or "")

    pr = await github.get_pr(row.repo, number)
    if pr is None or str(pr.get("state") or "") != "open":
        row.status = "closed"
        row.pending_trigger = ""
        await store.save(row)
        return {"status": "closed", "next": "resolve the task: the pull request is closed"}
    head = str((pr.get("head") or {}).get("sha") or "")
    if not reviewed_sha or head != reviewed_sha:
        row.status = "failed"
        row.error = "head moved during the review"
        row.head_sha = head
        row.pending_trigger = row.pending_trigger or "new_head"
        await store.save(row)
        return {
            "status": "stale",
            "next": "resolve the task: the head moved, the intake will queue the new head",
        }

    prior_severities = {
        int(i["comment_id"]): str(i.get("severity") or "")
        for i in row.last_review.get("issues") or []
        if isinstance(i.get("comment_id"), int)
    }
    ticket = await _ticket_key(cfg, github, row, pr) if cfg.require_ticket else None
    decision = decide_verdict(
        str(output.get("verdict") or ""),
        output.get("issues") or [],
        prior_issues=output.get("prior_issues") or [],
        prior_severities=prior_severities,
        blocking_event=cfg.blocking_event,
        require_ticket=cfg.require_ticket,
        ticket_key=ticket,
    )
    prior_for_body = [
        {
            "description": p.get("description", ""),
            "status": p.get("status", ""),
            "note": p.get("note", ""),
        }
        for p in output.get("prior_issues") or []
    ]
    posted = await poster.create_review(
        {
            "repo": row.repo,
            "number": number,
            "verdict": decision.verdict,
            "summary": str(output.get("summary") or ""),
            "issues": decision.issues,
            "commit_id": head,
            "prior_issues": prior_for_body,
        }
    )
    if posted.get("error"):
        return await _fail(store, chat, row, f"posting failed: {posted['error']}")
    review_id = posted.get("review_id")
    review_url = str(posted.get("url") or "")

    # Map the inline comments GitHub created back to the findings they carry.
    by_anchor = {(c.get("path"), c.get("line")): c.get("id") for c in posted.get("comments") or []}
    recorded = []
    for issue in decision.issues:
        entry = dict(issue)
        entry["comment_id"] = by_anchor.get((issue.get("path"), issue.get("line")))
        recorded.append(entry)

    replies = 0
    for prior in output.get("prior_issues") or []:
        cid = prior.get("comment_id")
        if not isinstance(cid, int) or cid not in prior_severities:
            continue
        label = _PRIOR_LABEL.get(str(prior.get("status")), str(prior.get("status")))
        note = str(prior.get("note") or "").strip()
        result = await poster.reply(
            row.repo, number, cid, f"**{label}**" + (f" — {note}" if note else "")
        )
        if not result.get("error"):
            replies += 1

    review_ids = list(row.review_ids)
    if isinstance(review_id, int):
        review_ids.append(review_id)
    resolved = 0
    if decision.verdict == "APPROVE" and review_ids:
        result = await poster.resolve(row.repo, number, review_ids)
        resolved = int(result.get("resolved") or 0)

    blocking = len(decision.blocking) + len(decision.prior_blocking)
    text = _verdict_text(decision.verdict, blocking)
    announced = await _announce(store, chat, row, text)
    if announced and decision.verdict == "APPROVE" and chat and row.chat_message:
        try:
            await chat.react(row.chat_message, "\U0001f44d")
        except Exception:  # noqa: BLE001 - a reaction is decoration
            logger.debug("pr_review approve reaction failed for %s", row.key)

    row.status = _STATUS_FOR_VERDICT[decision.verdict]
    row.last_reviewed_sha = head
    row.head_sha = head
    row.review_ids = review_ids
    row.last_review = {
        "verdict": decision.verdict,
        "model_verdict": decision.model_verdict,
        "summary": str(output.get("summary") or ""),
        "issues": recorded,
        "review_id": review_id,
        "url": review_url,
        "sha": head,
    }
    row.error = ""
    row.followup = False
    await store.save(row)

    digest = ""
    if cfg.telegram_digest:
        digest = f"PR review {row.repo}#{number}: {text} — {review_url}"
    return {
        "status": "posted",
        "review_url": review_url,
        "review_id": review_id,
        "verdict": decision.verdict,
        "model_verdict": decision.model_verdict,
        "verdict_overridden": decision.overridden,
        "reasons": decision.reasons,
        "blocking": blocking,
        "inline_count": posted.get("inline_count", 0),
        "thread_replies": replies,
        "threads_resolved": resolved,
        "chat_announced": announced,
        "digest": digest,
        "next": "resolve the task with review_url as the evidence"
        + ("; your final output is the digest line, verbatim" if digest else ""),
    }

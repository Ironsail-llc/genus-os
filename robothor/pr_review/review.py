"""Prepare a review job, and finalize its result — the two halves around Claude Code.

:func:`prepare` fetches the pull request's head into a local clone, builds the
review prompt (skill + the repository's review rules, read from the BASE
branch so a pull request cannot rewrite the rules it is reviewed against +
this PR), starts the read-only Claude Code job itself and binds the job id to
the row. The agent never sees or edits the job's arguments.

:func:`finalize` is the only way a review is posted, and it posts only the job
:func:`prepare` bound, once. It reads the job's structured output straight
from the coding job (the agent never relays it), validates it, redacts
anything credential-shaped, recomputes the verdict with
:mod:`robothor.pr_review.policy`, posts it, replies on our previous threads,
resolves our own threads on approval, announces in the Chat thread, and
records the state. The agent cannot change a verdict because it never handles
one.

**Exactly once.** ``reviewing -> posting`` is a compare-and-set on the row's
status and job id, taken before anything is posted; a second finalize for the
same job finds ``posting`` (and resumes only the steps not yet recorded) or a
final status (and returns the review already posted). ``github_create_review``
is itself idempotent per head and review footer — the second layer, for a
crash between GitHub accepting the review and the row recording it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol

from robothor.pr_review.checkout import ensure_checkout, read_at
from robothor.pr_review.policy import decide_verdict, extract_ticket_key, is_blocking
from robothor.pr_review.posting import decide_review
from robothor.pr_review.prompt import APPENDIX_PATHS, ReviewContext, build_review_prompt
from robothor.pr_review.schema import review_output_schema, validate_review_output
from robothor.pr_review.store import ACTIVE_STATUSES, FINAL_STATUSES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from robothor.pr_review.clients import ChatClient, GitHubPort
    from robothor.pr_review.config import ReviewerConfig
    from robothor.pr_review.store import PrReviewRow, PrReviewStore

logger = logging.getLogger(__name__)

__all__ = ["Poster", "finalize", "map_comment_ids", "prepare", "redact_output"]

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
#: Statuses prepare may claim a row from: anything not already bound to a job.
_PREPARABLE = frozenset({"pending", "queued", "failed", "skipped", "closed", *FINAL_STATUSES})


class Poster(Protocol):
    async def create_review(self, args: dict[str, Any]) -> dict[str, Any]: ...
    async def reply(self, repo: str, number: int, comment_id: int, body: str) -> dict[str, Any]: ...
    async def resolve(self, repo: str, number: int, review_ids: list[int]) -> dict[str, Any]: ...


def _redact(text: Any) -> str:
    from robothor.secrets.redaction import redact

    return redact(str(text or ""))


def redact_output(output: Mapping[str, Any]) -> dict[str, Any]:
    """The review output with every free-text field passed through the secret redactor.

    Claude Code read the repository and ran ``gh``; whatever it quotes back —
    a token in a test fixture, a key in a log — would otherwise be posted to
    GitHub and Chat verbatim.
    """
    out = dict(output)
    out["summary"] = _redact(output.get("summary"))
    out["issues"] = [
        {**i, "title": _redact(i.get("title")), "body": _redact(i.get("body"))}
        for i in output.get("issues") or []
    ]
    out["prior_issues"] = [
        {**p, "description": _redact(p.get("description")), "note": _redact(p.get("note"))}
        for p in output.get("prior_issues") or []
    ]
    return out


def map_comment_ids(
    issues: Sequence[Mapping[str, Any]], posted: Mapping[str, Any]
) -> list[int | None]:
    """The inline comment id GitHub gave each finding, by position; None for body-only ones.

    Only blocking findings are posted inline, in their order, and only those
    whose anchor the diff can carry; GitHub returns the created comments in
    posting order. So the n-th posted comment belongs to the next blocking
    finding (in order) whose anchor matches it. Matching by anchor alone would
    hand two findings on one line the same id, and let a nit on that line
    overwrite a blocker's severity.
    """
    comments: list[Mapping[str, Any]] = list(posted.get("comments") or [])
    if not comments:
        comments = [{"id": cid} for cid in posted.get("comment_ids") or []]
    out: list[int | None] = [None] * len(issues)
    candidates = [n for n, i in enumerate(issues) if is_blocking(i) and i.get("line")]
    pos = 0
    for comment in comments:
        cid = comment.get("id")
        if not isinstance(cid, int) or isinstance(cid, bool):
            continue
        anchored = "path" in comment
        for k in range(pos, len(candidates)):
            issue = issues[candidates[k]]
            if not anchored or (
                issue.get("path") == comment.get("path")
                and issue.get("line") == comment.get("line")
            ):
                out[candidates[k]] = cid
                pos = k + 1
                break
    return out


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


def _bound(row: PrReviewRow, head: str) -> dict[str, Any]:
    return {
        "repo": row.repo,
        "number": row.number,
        "head_sha": head,
        "job_id": row.job_id,
        "already_started": True,
        "next": "call claude_code_wait with this job_id, then pr_review_finalize",
    }


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
    start_job: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
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
    head = str((pr.get("head") or {}).get("sha") or "")
    if row.status == "reviewing" and row.job_id and row.queued_sha == head:
        return _bound(row, head)  # a retried prepare: the job is already running
    if row.status == "posting":
        return {"error": "a finished review for this pull request is being posted; wait"}
    if str(pr.get("state") or "") != "open":
        row.status = "closed"
        row.pending_trigger = ""
        await store.save(row)
        return {"skip": True, "reason": "pull request is closed; resolve the task"}
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
    # The rules a pull request is reviewed against come from the branch it
    # merges into: a PR that edits CLAUDE.md must not write its own review rules.
    appendix, appendix_path = None, ""
    for candidate in APPENDIX_PATHS:
        appendix = await reader(path, f"origin/{base_ref}", candidate)
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

    # Claim the row before spending money on a job: one job per row at a time.
    previous_status = row.status
    claimed = await store.transition(
        tenant_id,
        row.repo,
        number,
        from_statuses=_PREPARABLE,
        to_status="reviewing",
        set_job_id="",
    )
    if claimed is None:
        current = await store.get(tenant_id, row.repo, number)
        if current and current.status == "reviewing" and current.job_id:
            return _bound(current, current.queued_sha or head)
        return {"error": "another prepare holds this pull request; try again shortly"}
    started = await start_job(start_args)
    job_id = str(started.get("job_id") or "")
    if started.get("error") or not job_id:
        await store.transition(
            tenant_id,
            row.repo,
            number,
            from_statuses=("reviewing",),
            to_status=previous_status,
            job_id="",
        )
        return {"error": f"claude_code_start failed: {started.get('error') or 'no job id'}"}
    claimed.job_id = job_id
    claimed.head_sha = head
    claimed.queued_sha = head
    claimed.mode = ctx.mode
    await store.save(claimed)
    return {
        "repo": row.repo,
        "number": number,
        "head_sha": head,
        "mode": ctx.mode,
        "since_sha": ctx.since_sha if ctx.mode == "incremental" else "",
        "depth": ctx.depth,
        "job_id": job_id,
        "next": "call claude_code_wait with this job_id until it is done, then "
        "pr_review_finalize with the same job_id",
    }


# ── finalize ────────────────────────────────────────────────────────────


def _chat_line(row: PrReviewRow, text: str) -> str:
    url = row.url or f"https://github.com/{row.repo}/pull/{row.number}"
    return f"<{url}|#{row.number}>: {_redact(text)}"


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


def _digest(cfg: ReviewerConfig, row: PrReviewRow, text: str) -> str:
    return f"PR review {row.repo}#{row.number}: {text}" if cfg.telegram_digest else ""


async def _fail(
    cfg: ReviewerConfig,
    store: PrReviewStore,
    chat: ChatClient | None,
    row: PrReviewRow,
    reason: str,
) -> dict[str, Any]:
    from datetime import UTC, datetime

    reason = _redact(reason)
    claimed = await store.transition(
        row.tenant_id,
        row.repo,
        row.number,
        from_statuses=("reviewing", "posting"),
        to_status="failed",
        job_id=row.job_id,
    )
    if claimed is None:
        return {"error": f"not recorded: the review is no longer in progress ({reason[:200]})"}
    claimed.error = reason[:_ERROR_MAX]
    claimed.attempts += 1
    claimed.failed_at = datetime.now(UTC)
    await store.save(claimed)
    announced = await _announce(
        store,
        chat,
        claimed,
        f'Automated review failed: {reason[:300]}. Reply "re-review" to retry.',
    )
    return {
        "status": "failed",
        "error": reason,
        # Without a Chat thread the operator's digest is the only place this surfaces.
        "digest": "" if announced else _digest(cfg, claimed, f"review failed — {reason[:200]}"),
        "next": "resolve the task with this error",
    }


def _verdict_text(verdict: str, blocking: int) -> str:
    if verdict == "APPROVE":
        return "Approved"
    if blocking:
        return f"Needs changes — {_plural(blocking, 'blocking finding')}"
    return "Comments, nothing blocking"


def _already_posted(cfg: ReviewerConfig, row: PrReviewRow) -> dict[str, Any]:
    last = row.last_review
    return {
        "status": "posted",
        "already_posted": True,
        "review_url": str(last.get("url") or ""),
        "review_id": last.get("review_id"),
        "verdict": str(last.get("verdict") or ""),
        "digest": "",
        "next": "resolve the task with review_url as the evidence; it was already posted",
    }


def _job_problem(job: Any, row: PrReviewRow) -> str:
    job_id = str(getattr(job, "id", "") or "")
    if not row.job_id or job_id != row.job_id:
        return (
            f"job {job_id or '?'} is not the job prepared for {row.key}; "
            "finalize only the job_id pr_review_prepare returned"
        )
    if getattr(job, "mode", "review") != "review":
        return "only a mode=review coding job can be finalized as a review"
    reviewed = str(getattr(job, "base_sha", "") or "")
    if row.queued_sha and reviewed and reviewed != row.queued_sha:
        return f"job reviewed {reviewed[:12]}, but {row.queued_sha[:12]} was prepared"
    return ""


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
        if row.status in ("reviewing", "posting"):
            return {"error": "a review job is bound to this pull request; finalize it by job_id"}
        row.status = (
            _STATUS_FOR_VERDICT.get(str(row.last_review.get("verdict") or ""), "commented")
            if row.last_reviewed_sha
            else "pending"
        )
        await store.save(row)
        return {"status": "dismissed", "next": "resolve the task: not a re-review request"}

    if job is None:
        return {"error": "coding job not found for this tenant"}
    problem = _job_problem(job, row)
    if problem:
        return {"error": problem}
    job_id = row.job_id
    if row.status in FINAL_STATUSES and row.last_review.get("job_id") == job_id:
        return _already_posted(cfg, row)
    if row.status not in ("reviewing", "posting"):
        return {"error": f"{row.key} is {row.status or 'untracked'}, not under review"}

    status = str(getattr(job, "status", ""))
    if status in ("queued", "running"):
        return {"error": "the review job is still running; call claude_code_wait again"}
    if row.status == "reviewing" and status != "done":
        return await _fail(
            cfg, store, chat, row, f"review job {status}: {getattr(job, 'error', '')}"
        )

    raw = (getattr(job, "result", None) or {}).get("structured_output")
    problems = validate_review_output(raw)
    if problems:
        return await _fail(
            cfg, store, chat, row, "invalid review output: " + "; ".join(problems[:5])
        )
    assert isinstance(raw, dict)
    output = redact_output(raw)
    reviewed_sha = str(getattr(job, "base_sha", "") or "")

    if row.status == "reviewing":
        pr = await github.get_pr(row.repo, number)
        if pr is None or str(pr.get("state") or "") != "open":
            closed = await store.transition(
                tenant_id,
                row.repo,
                number,
                from_statuses=("reviewing",),
                to_status="closed",
                job_id=job_id,
            )
            if closed is not None:
                closed.pending_trigger = ""
                await store.save(closed)
            return {"status": "closed", "next": "resolve the task: the pull request is closed"}
        head = str((pr.get("head") or {}).get("sha") or "")
        if not reviewed_sha or head != reviewed_sha:
            stale = await store.transition(
                tenant_id,
                row.repo,
                number,
                from_statuses=("reviewing",),
                to_status="failed",
                job_id=job_id,
            )
            if stale is not None:
                from datetime import UTC, datetime

                stale.error = "head moved during the review"
                stale.head_sha = head
                stale.failed_at = datetime.now(UTC)
                stale.pending_trigger = stale.pending_trigger or "new_head"
                await store.save(stale)
            return {
                "status": "stale",
                "digest": ""
                if row.chat_thread
                else _digest(cfg, row, "head moved during the review; re-queued"),
                "next": "resolve the task: the head moved, the intake will queue the new head",
            }
        ticket = await _ticket_key(cfg, github, row, pr) if cfg.require_ticket else None
        # The one step that decides who posts: reviewing -> posting, for this job only.
        claimed = await store.transition(
            tenant_id,
            row.repo,
            number,
            from_statuses=("reviewing",),
            to_status="posting",
            job_id=job_id,
        )
        if claimed is None:
            current = await store.get(tenant_id, row.repo, number)
            if current and current.last_review.get("job_id") == job_id:
                return _already_posted(cfg, current)
            return {"error": "another finalize is posting this review; it posts once"}
        row = claimed
        prior_issues_seen = list(row.last_review.get("issues") or [])
        context = {
            "prior_issues_seen": prior_issues_seen,
            "prior_review_ids": list(row.review_ids),
            "ticket": ticket,
        }
        row.last_review = {**row.last_review, "pending": {"job_id": job_id, **context}}
        await store.save(row)
    else:
        context = dict(row.last_review.get("pending") or {})
        if not context:  # posting, but the record is from a finished post
            context = {
                "prior_issues_seen": list(row.last_review.get("prior_issues_seen") or []),
                "prior_review_ids": list(row.last_review.get("prior_review_ids") or []),
                "ticket": row.last_review.get("ticket"),
            }
    return await _post(
        cfg,
        store,
        tenant_id,
        row,
        output,
        reviewed_sha,
        context,
        job_id=job_id,
        poster=poster,
        chat=chat,
    )


def _prior_severities(issues: list[dict[str, Any]]) -> dict[int, str]:
    out: dict[int, str] = {}
    for issue in issues:
        cid = issue.get("comment_id")
        if not isinstance(cid, int) or isinstance(cid, bool):
            continue
        severity = str(issue.get("severity") or "").lower()
        # A comment id carries the severity of the finding it was posted for;
        # never let a later, weaker entry with the same id downgrade it.
        if out.get(cid) in ("blocker", "major") and severity not in ("blocker", "major"):
            continue
        out[cid] = severity
    return out


async def _post(
    cfg: ReviewerConfig,
    store: PrReviewStore,
    tenant_id: str,
    row: PrReviewRow,
    output: dict[str, Any],
    head: str,
    context: dict[str, Any],
    *,
    job_id: str,
    poster: Poster,
    chat: ChatClient | None,
) -> dict[str, Any]:
    """Post a claimed review; every step is recorded so a retry resumes after it."""
    prior_severities = _prior_severities(list(context.get("prior_issues_seen") or []))
    decision = decide_verdict(
        str(output.get("verdict") or ""),
        output.get("issues") or [],
        prior_issues=output.get("prior_issues") or [],
        prior_severities=prior_severities,
        blocking_event=cfg.blocking_event,
        require_ticket=cfg.require_ticket,
        ticket_key=context.get("ticket"),
    )
    pending = row.last_review.get("pending") or {}
    done: dict[str, Any] = dict(pending.get("done") or {})

    def _record(step: str, value: Any) -> dict[str, Any]:
        done[step] = value
        return {
            **row.last_review,
            "pending": {**pending, **context, "job_id": job_id, "done": done},
        }

    if "review" not in done:
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
                "number": row.number,
                "verdict": decision.verdict,
                "summary": str(output.get("summary") or ""),
                "issues": decision.issues,
                "commit_id": head,
                "prior_issues": prior_for_body,
            }
        )
        if posted.get("error"):
            return await _fail(cfg, store, chat, row, f"posting failed: {posted['error']}")
        ids = map_comment_ids(decision.issues, posted)
        recorded = [
            {**issue, "comment_id": cid} for issue, cid in zip(decision.issues, ids, strict=True)
        ]
        row.last_review = _record(
            "review",
            {
                "review_id": posted.get("review_id"),
                "url": str(posted.get("url") or ""),
                "inline_count": posted.get("inline_count", 0),
                "issues": recorded,
            },
        )
        await store.save(row)
    review = done["review"]
    review_id = review.get("review_id")

    if "replies" not in done:
        replies = 0
        for prior in output.get("prior_issues") or []:
            cid = prior.get("comment_id")
            if not isinstance(cid, int) or cid not in prior_severities:
                continue
            label = _PRIOR_LABEL.get(str(prior.get("status")), str(prior.get("status")))
            note = str(prior.get("note") or "").strip()
            result = await poster.reply(
                row.repo, row.number, cid, f"**{label}**" + (f" — {note}" if note else "")
            )
            if not result.get("error"):
                replies += 1
        row.last_review = _record("replies", replies)
        await store.save(row)

    review_ids = [int(i) for i in context.get("prior_review_ids") or []]
    if isinstance(review_id, int):
        review_ids.append(review_id)
    if "resolved" not in done:
        resolved = 0
        # APPROVE implies every previous blocking finding was reported resolved
        # (policy), so every thread we opened may be closed — and only then.
        if decision.verdict == "APPROVE" and review_ids:
            result = await poster.resolve(row.repo, row.number, review_ids)
            resolved = int(result.get("resolved") or 0)
        row.last_review = _record("resolved", resolved)
        await store.save(row)

    blocking = len(decision.blocking) + len(decision.prior_blocking)
    text = _verdict_text(decision.verdict, blocking)
    if "announced" not in done:
        announced = await _announce(store, chat, row, text)
        if announced and decision.verdict == "APPROVE" and chat and row.chat_message:
            try:
                await chat.react(row.chat_message, "\U0001f44d")
            except Exception:  # noqa: BLE001 - a reaction is decoration
                logger.debug("pr_review approve reaction failed for %s", row.key)
        row.last_review = _record("announced", announced)
        await store.save(row)

    final = await store.transition(
        tenant_id,
        row.repo,
        row.number,
        from_statuses=("posting",),
        to_status=_STATUS_FOR_VERDICT[decision.verdict],
        job_id=job_id,
    )
    if final is None:
        current = await store.get(tenant_id, row.repo, row.number)
        if current and current.last_review.get("job_id") == job_id:
            return _already_posted(cfg, current)
        return {"error": "the review was posted but its state changed underneath; check the PR"}
    review_url = str(review.get("url") or "")
    final.last_reviewed_sha = head
    final.head_sha = head
    final.review_ids = review_ids
    final.last_review = {
        "verdict": decision.verdict,
        "model_verdict": decision.model_verdict,
        "summary": str(output.get("summary") or ""),
        "issues": review.get("issues") or [],
        "review_id": review_id,
        "url": review_url,
        "sha": head,
        "job_id": job_id,
        "prior_issues_seen": list(context.get("prior_issues_seen") or []),
        "prior_review_ids": list(context.get("prior_review_ids") or []),
        "ticket": context.get("ticket"),
    }
    final.error = ""
    final.attempts = 0
    final.failed_at = None
    final.followup = False
    await store.save(final)

    return {
        "status": "posted",
        "review_url": review_url,
        "review_id": review_id,
        "verdict": decision.verdict,
        "model_verdict": decision.model_verdict,
        "verdict_overridden": decision.overridden,
        "reasons": decision.reasons,
        "blocking": blocking,
        "inline_count": review.get("inline_count", 0),
        "thread_replies": done.get("replies", 0),
        "threads_resolved": done.get("resolved", 0),
        "chat_announced": done.get("announced", False),
        "digest": _digest(cfg, final, f"{text} — {review_url}"),
        "next": "resolve the task with review_url as the evidence"
        + ("; your final output is the digest line, verbatim" if cfg.telegram_digest else ""),
    }

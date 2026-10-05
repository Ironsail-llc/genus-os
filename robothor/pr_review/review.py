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

from robothor.pr_review.checkout import ensure_checkout, merge_conflicts, read_at
from robothor.pr_review.context import checks as check_list
from robothor.pr_review.context import discussion as discussion_items
from robothor.pr_review.context import linked_issues, pr_description
from robothor.pr_review.policy import decide_verdict, extract_ticket_key, is_blocking
from robothor.pr_review.posting import decide_review
from robothor.pr_review.prompt import (
    APPENDIX_PATHS,
    ReviewContext,
    build_review_prompt,
    load_guidelines,
)
from robothor.pr_review.schema import review_output_schema, validate_review_output
from robothor.pr_review.store import ACTIVE_STATUSES, FINAL_STATUSES
from robothor.pr_review.ticket import ticket_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from robothor.pr_review.clients import ChatClient, GitHubPort

    #: ``(ticket prefix, PR title) -> [{key, summary, status}]``.
    TicketSearch = Callable[[str, str], Awaitable[list[dict[str, Any]]]]
    from robothor.pr_review.config import ReviewerConfig
    from robothor.pr_review.store import PrReviewRow, PrReviewStore
    from robothor.pr_review.ticket import TicketFetcher

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
#: ``PrReviewRow.source`` of a review the operator asked for (pr_review_intake(pr=...)).
OPERATOR_SOURCE = "operator"
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
    """The ticket key in the title, branch or body, else in a commit message or trailer."""
    prefixes = cfg.prefixes_for(row.repo)
    title, branch, body = _pr_text(pr)
    key = extract_ticket_key(prefixes, title=title, branch=branch, body=body)
    if key:
        return key
    commits = await github.list_commits(row.repo, row.number)
    messages = [str((c.get("commit") or {}).get("message") or "") for c in commits]
    return extract_ticket_key(prefixes, commit_messages=messages)


# ── prepare ─────────────────────────────────────────────────────────────

#: The second round a deep review gets when its first used under 60% of its
#: turns: same session, so it re-checks rather than starts over.
COMPLETENESS_PASS = (
    "Completeness pass. Before answering again: (1) name the lens groups you spent the "
    "least time on and walk them again over the whole diff; (2) for every behaviour this "
    "pull request changes, check the tests: is there one, does CI run it, would it fail if "
    "the change were reverted, and does a test on the base branch break or stop checking "
    "anything once this merges; (3) re-read every claim, runbook step and command in the "
    "PR description and every point in <pr_discussion> against the code; (4) re-check the "
    "GitHub state section; (5) re-check each finding's severity against the severity "
    "rules: anything that can move money twice, lose or corrupt data, expose another "
    "tenant's data or break the base branch on merge is at least major, whatever its "
    "reach. Add what you missed, drop what you can now disprove, and return the FULL "
    "structured result again: every finding, not only new ones."
)
_EFFORT_RANK = ("low", "medium", "high", "xhigh", "max")
_CANDIDATES_MAX = 5


def _effort(cfg: ReviewerConfig, deep: bool) -> str:
    """The configured effort, raised to the deep effort on a deep review, never lowered."""
    base = cfg.review_effort
    if not deep or cfg.deep_effort not in _EFFORT_RANK:
        return base
    if base in _EFFORT_RANK and _EFFORT_RANK.index(base) >= _EFFORT_RANK.index(cfg.deep_effort):
        return base
    return cfg.deep_effort


async def _optional(github: Any, name: str, *args: Any) -> Any:
    """An optional GitHub read; None when the port lacks it or it fails."""
    method = getattr(github, name, None)
    if method is None:
        return None
    try:
        return await method(*args)
    except Exception as exc:  # noqa: BLE001 - context is extra; the review goes ahead
        logger.info("pr_review: %s failed: %s", name, type(exc).__name__)
        return None


async def _pr_extras(
    cfg: ReviewerConfig, github: Any, repo: str, number: int, head: str
) -> dict[str, Any]:
    """What reviewers already said, and the head commit's checks (None when unknown)."""
    reviews = await _optional(github, "list_reviews", repo, number)
    comments = await _optional(github, "list_review_comments", repo, number)
    issue_comments = await _optional(github, "list_issue_comments", repo, number)
    raw_checks = await _optional(github, "list_checks", repo, head)
    exclude = (cfg.bot_login,) if cfg.bot_login else ()
    return {
        "discussion": discussion_items(
            reviews or [], comments or [], issue_comments or [], exclude_logins=exclude
        ),
        "checks": (check_list(raw_checks, raw_checks) if isinstance(raw_checks, dict) else None),
    }


async def _ticket_candidates(
    cfg: ReviewerConfig, repo: str, title: str, search: TicketSearch
) -> tuple[dict[str, Any], ...]:
    prefixes = cfg.prefixes_for(repo)
    if not prefixes or not title.strip():
        return ()
    try:
        found = await search(prefixes[0], title)
    except Exception as exc:  # noqa: BLE001 - candidates are a courtesy
        logger.info("pr_review: ticket search failed: %s", type(exc).__name__)
        return ()
    out = [
        {
            "key": str(t["key"]),
            "summary": _redact(t.get("summary"))[:200],
            "status": str(t.get("status") or ""),
        }
        for t in found or []
        if isinstance(t, dict) and t.get("key")
    ]
    return tuple(out[:_CANDIDATES_MAX])


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
    fetch_ticket: TicketFetcher | None = None,
    search_tickets: TicketSearch | None = None,
    conflicts: Callable[[Path, str, str], Awaitable[list[str] | None]] | None = None,
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
    ticket_key = await _ticket_key(cfg, github, row, pr)
    ticket = await ticket_context(ticket_key, fetch_ticket) if ticket_key else None
    candidates: tuple[dict[str, Any], ...] = ()
    if not ticket_key and search_tickets is not None:
        candidates = await _ticket_candidates(cfg, row.repo, title, search_tickets)
    guidelines = load_guidelines(cfg.guidelines_path, skill_text)
    extras = await _pr_extras(cfg, github, row.repo, number, head)
    conflicted = await (conflicts or merge_conflicts)(path, f"origin/{base_ref}", head)
    changed = int(pr.get("additions") or 0) + int(pr.get("deletions") or 0)
    deep = (row.depth or "full") != "light" and changed >= cfg.deep_lines
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
        ticket_key=ticket_key,
        ticket=ticket,
        appendix=appendix,
        appendix_path=appendix_path,
        previous_verdict=str(row.last_review.get("verdict") or ""),
        previous_summary=str(row.last_review.get("summary") or ""),
        previous_issues=previous,
        description=pr_description(pr),
        labels=tuple(str((lab or {}).get("name") or "") for lab in pr.get("labels") or [] if lab),
        linked_issues=linked_issues(str(pr.get("body") or ""), own_repo=row.repo),
        discussion=extras["discussion"],
        mergeable=pr.get("mergeable") if isinstance(pr.get("mergeable"), bool) else None,
        mergeable_state=str(pr.get("mergeable_state") or ""),
        conflicts=tuple(conflicted) if conflicted is not None else None,
        checks=extras["checks"],
        changed_lines=changed,
        deep=deep,
        ticket_candidates=candidates,
    )
    acceptance: dict[str, Any] = {"require_commit": False}
    if deep:
        acceptance["second_pass"] = COMPLETENESS_PASS
        acceptance["second_pass_below_turns"] = max(1, int(cfg.review_max_turns * 0.6))
    start_args: dict[str, Any] = {
        "task": build_review_prompt(guidelines, ctx),
        "repo_path": str(path),
        "acceptance": acceptance,
        "mode": "review",
        "base_ref": head,
        "json_schema": review_output_schema(),
        "grant_github": True,
        "max_budget_usd": cfg.review_budget_usd,
        "max_rounds": 2,
        "max_turns": cfg.review_max_turns,
        "round_timeout_s": cfg.review_round_timeout_s,
    }
    effort = _effort(cfg, deep)
    if effort:
        start_args["effort"] = effort
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
        "ticket": ticket_key or "",
        "ticket_state": ticket.state if ticket else "none",
        "guidelines": guidelines.source,
        "deep": deep,
        "github_state": {
            "mergeable_state": ctx.mergeable_state,
            "conflicts": list(conflicted or []),
            "failing_checks": [c.name for c in ctx.checks or () if c.failing],
        },
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
    """The Telegram line: with the digest on, or for a review the operator asked for."""
    wanted = cfg.telegram_digest or row.source == OPERATOR_SOURCE
    return f"PR review {row.repo}#{row.number}: {text}" if wanted else ""


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
    short = reason if len(reason) <= 300 else f"{reason[:300]}\u2026"
    announced = await _announce(
        store,
        chat,
        claimed,
        f"\u26a0\ufe0f Automated review failed after {_plural(claimed.attempts, 'attempt')}: "
        f'{short}\nReply "re-review" to try again.',
    )
    return {
        "status": "failed",
        "error": reason,
        # Without a Chat thread the operator's digest is the only place this surfaces.
        "digest": "" if announced else _digest(cfg, claimed, f"review failed — {reason[:200]}"),
        "next": "resolve the task with this error",
    }


def _verdict_text(verdict: str, blocking: int) -> str:
    """The thread reply, in the wording reviewers already know, plus the blocking count."""
    if verdict == "APPROVE":
        return "Approved"
    return "Comments/change request" + (f" \u2014 {blocking} blocking" if blocking else "")


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


async def _swap_reactions(cfg: ReviewerConfig, chat: ChatClient | None, row: PrReviewRow) -> None:
    """On approval the approved reaction replaces our claim reaction on the PR message."""
    if not (chat and row.chat_message and cfg.approved_reaction):
        return
    try:
        await chat.react(row.chat_message, cfg.approved_reaction)
        if cfg.claim_reaction and cfg.claim_reaction != cfg.approved_reaction:
            removed = await chat.unreact(row.chat_message, cfg.claim_reaction, cfg.chat_self_users)
            if not removed:
                logger.info("pr_review: could not remove the claim reaction on %s", row.key)
    except Exception:  # noqa: BLE001 - a reaction is decoration
        logger.debug("pr_review approve reaction failed for %s", row.key)


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
        if decision.verdict == "APPROVE":
            await _swap_reactions(cfg, chat, row)
        announced = await _announce(store, chat, row, text)
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
    # ``followup`` stays: a request made during this review is still pending,
    # and the intake clears it when it dispatches (or quietly skips) that one.
    await store.save(final)

    digest = _digest(cfg, final, f"{text} \u2014 {review_url}")
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
        "digest": digest,
        "next": "resolve the task with review_url as the evidence"
        + ("; your final output is the digest line, verbatim" if digest else ""),
    }

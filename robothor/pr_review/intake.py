"""Deterministic review intake: find pull requests that need a review, file one task each.

Run by the ``pr_review_intake`` tool from a cron workflow. No model is called
anywhere in here; every decision is a rule.

**Sources.**

1. GitHub — every open, non-draft pull request in the configured repositories
   (``watch_repos``), plus pull requests requesting the bot login's review —
   in the configured repositories only (a request from a fork or any other
   repository the token can read is ignored). A
   pull request whose head differs from the head we last reviewed (and from the
   head a task was already filed for) gets a trigger.
2. Google Chat — messages in the configured space since the stored cursor. A
   top-level message linking a pull request in an allowed repository claims it
   (reaction) and triggers an initial review. A thread reply from the person
   who posted the link is classified by :func:`classify_rereview`: a clear
   request triggers a re-review; an ambiguous one is handed to the agent as a
   task to decide; a reviewer's status line ("#12: Approved") does nothing. In
   a thread that posted several pull requests, a reply naming some of them
   ("12 is ready for re-review") re-reviews only those. Every message is
   handled once; one that raises is recorded with its error and skipped —
   never retried forever.

**Dispatch.** Each pending trigger becomes at most ONE CRM task for the pull
request's current head, deduplicated by repo+number+sha, up to
``max_concurrent`` open review tasks. :func:`decide_review` decides full,
incremental or skip by SHA; :class:`DepthPolicy` skips lockfile-only and bot
changes. A trigger that arrives while a review is running is kept and
dispatched after that review is finalized. A Chat-posted pull request that is
closed, a draft or skipped gets a thread reply saying so.

**One writer.** A run holds the store's per-tenant intake lock; a concurrent
run returns ``{"skipped": "locked"}`` and changes nothing. Only the intake
dispatches: the pr-review-run workflow calls it with ``count_only`` to decide
whether to wake the agent.

**Retries.** A failed review (job failure, stale, posting error) is retried
on the same head after ``retry_cooldown_minutes``, at most
:data:`MAX_ATTEMPTS` times per head; a new head starts over.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from robothor.pr_review.classify import (
    classify_rereview,
    match_allowed_prs,
    mentioned_numbers,
    message_search_text,
)
from robothor.pr_review.depth import Depth, DepthPolicy
from robothor.pr_review.posting import decide_review
from robothor.pr_review.store import ACTIVE_STATUSES, PrReviewRow
from robothor.pr_review.tasks import ReviewTaskSpec

if TYPE_CHECKING:
    from robothor.pr_review.clients import ChatClient, GitHubPort
    from robothor.pr_review.config import ReviewerConfig
    from robothor.pr_review.store import PrReviewStore
    from robothor.pr_review.tasks import TaskSink

logger = logging.getLogger(__name__)

__all__ = ["MAX_ATTEMPTS", "Intake", "parse_chat_time"]

#: Strongest first. A weaker trigger never replaces a stronger pending one.
_TRIGGER_RANK = {"initial": 4, "rereview": 3, "new_head": 2, "retry": 2, "ambiguous": 1, "": 0}
#: Failed review attempts on one head before the intake stops retrying it.
MAX_ATTEMPTS = 3
_DRAFT_NOTE = "draft: waiting until it is ready for review"
#: Re-read this much before the cursor: Chat's createTime filter is strict and
#: clocks are not; per-message dedupe makes the overlap free.
_CURSOR_OVERLAP = timedelta(seconds=60)
_CHAT_SOURCE = "chat"
_ERROR_MAX = 500


def parse_chat_time(value: str) -> datetime | None:
    """RFC 3339 from Google Chat (``...Z``, up to nanoseconds) as an aware datetime."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits) :]
        text = f"{head}.{digits[:6]}{tz}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _format_chat_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def trigger_is_retry(row: PrReviewRow) -> bool:
    return row.pending_trigger == "retry"


def _raise_trigger(row: PrReviewRow, trigger: str, text: str = "") -> bool:
    if _TRIGGER_RANK[trigger] <= _TRIGGER_RANK.get(row.pending_trigger, 0):
        return False
    row.pending_trigger = trigger
    row.trigger_text = text if trigger == "ambiguous" else ""
    if row.status in ACTIVE_STATUSES:
        row.followup = True
    return True


class Intake:
    def __init__(
        self,
        cfg: ReviewerConfig,
        store: PrReviewStore,
        tenant_id: str,
        *,
        tasks: TaskSink,
        github: GitHubPort | None = None,
        chat: ChatClient | None = None,
        now: datetime | None = None,
        depth_policy: DepthPolicy | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.tenant_id = tenant_id
        self.tasks = tasks
        self.github = github
        self.chat = chat
        self.now = now or datetime.now(UTC)
        self.depth_policy = depth_policy or DepthPolicy(skip_labels=cfg.skip_labels)
        self.summary: dict[str, Any] = {
            "discovered": 0,
            "chat_messages": 0,
            "chat_errors": 0,
            "triggers": 0,
            "tasks_created": 0,
            "deferred": 0,
            "skipped": 0,
            "closed": 0,
            "no_new_commits": 0,
            "stale": 0,
            "retries": 0,
            "errors": [],
        }

    def _error(self, where: str, exc: BaseException | str) -> None:
        msg = f"{where}: {exc}"[:_ERROR_MAX]
        logger.warning("pr_review intake %s", msg)
        self.summary["errors"].append(msg)

    def _new_row(self, repo: str, number: int, source: str) -> PrReviewRow:
        return PrReviewRow(
            tenant_id=self.tenant_id,
            repo=repo,
            number=number,
            url=f"https://github.com/{repo}/pull/{number}",
            source=source,
        )

    async def run(self, *, poll: bool = True, count_only: bool = False) -> dict[str, Any]:
        if count_only:
            return await self._counts()
        async with self.store.intake_lock(self.tenant_id) as held:
            if not held:
                return {"skipped": "locked"}
            if poll:
                if self.github is None and (self.cfg.repos or self.cfg.bot_login):
                    self._error("github", "GITHUB_TOKEN not configured")
                elif self.github is not None:
                    await self._poll_github()
                if self.cfg.chat_space and self.chat is not None:
                    await self._poll_chat()
            await self._expire_stale()
            await self._retry_failed()
            if self.github is not None:
                await self._dispatch()
            return await self._counts()

    async def _counts(self) -> dict[str, Any]:
        active = await self.store.list_active(self.tenant_id)
        pending = await self.store.list_pending(self.tenant_id)
        self.summary["open_tasks"] = len(active)
        self.summary["queued_tasks"] = sum(1 for r in active if r.status == "queued")
        self.summary["pending"] = len(pending)
        return self.summary

    # ── GitHub ──────────────────────────────────────────────────────

    async def _poll_github(self) -> None:
        assert self.github is not None
        candidates: dict[tuple[str, int], tuple[str, str, dict[str, Any] | None]] = {}
        if self.cfg.watch_repos:
            for repo in self.cfg.repos:
                try:
                    prs = await self.github.list_open_prs(repo)
                except Exception as exc:  # noqa: BLE001 - one repo must not stop the others
                    self._error(f"github list {repo}", exc)
                    continue
                for listed in prs:
                    if listed.get("number"):
                        key = (repo.lower(), int(listed["number"]))
                        candidates[key] = (repo, "github", listed)
        if self.cfg.bot_login:
            try:
                requested = await self.github.review_requested(self.cfg.bot_login)
            except Exception as exc:  # noqa: BLE001
                self._error("github review-requested search", exc)
                requested = []
            for repo, number in requested:
                if (repo.lower(), number) in candidates or not self.cfg.allows_repo(repo):
                    continue
                try:
                    found = await self.github.get_pr(repo, number)
                except Exception as exc:  # noqa: BLE001
                    self._error(f"github get {repo}#{number}", exc)
                    continue
                candidates[(repo.lower(), number)] = (repo, "review_request", found)

        for (_key, number), (repo, source, pr) in candidates.items():
            if not pr or pr.get("draft") or str(pr.get("state") or "open") != "open":
                continue
            head = str((pr.get("head") or {}).get("sha") or "")
            if not head:
                continue
            row = await self.store.get(self.tenant_id, repo, number)
            if row is None:
                row = self._new_row(repo, number, source)
                self.summary["discovered"] += 1
            if row.status == "closed":
                row.status = "pending"
            row.head_sha = head
            row.title = str(pr.get("title") or row.title)
            row.author = str((pr.get("user") or {}).get("login") or row.author)
            row.url = str(pr.get("html_url") or row.url)
            if head != row.last_reviewed_sha and head != row.queued_sha:
                trigger = "new_head" if row.last_reviewed_sha else "initial"
                if _raise_trigger(row, trigger):
                    self.summary["triggers"] += 1
            await self.store.save(row)

    # ── Google Chat ─────────────────────────────────────────────────

    async def _poll_chat(self) -> None:
        assert self.chat is not None
        space = self.cfg.chat_space
        cursor = await self.store.get_cursor(self.tenant_id, _CHAT_SOURCE)
        start = parse_chat_time(cursor)
        since = (
            start - _CURSOR_OVERLAP
            if start
            else self.now - timedelta(minutes=self.cfg.chat_lookback_minutes)
        )
        try:
            messages = await self.chat.list_messages(space, _format_chat_time(since))
        except Exception as exc:  # noqa: BLE001 - the GitHub half still runs
            self._error("chat list", exc)
            return
        newest = start
        for message in sorted(messages, key=lambda m: str(m.get("createTime") or "")):
            name = str(message.get("name") or "")
            created = parse_chat_time(str(message.get("createTime") or ""))
            if created and (newest is None or created > newest):
                newest = created
            if not name or await self.store.message_seen(self.tenant_id, name):
                continue
            self.summary["chat_messages"] += 1
            try:
                kind, outcome = await self._handle_message(message)
            except Exception as exc:  # noqa: BLE001 - record it and move on, never wedge
                self.summary["chat_errors"] += 1
                self._error(f"chat message {name}", exc)
                await self.store.record_message(
                    self.tenant_id, name, "error", "", f"{type(exc).__name__}: {exc}"
                )
                continue
            await self.store.record_message(self.tenant_id, name, kind, outcome)
        if newest is not None and (start is None or newest > start):
            await self.store.set_cursor(self.tenant_id, _CHAT_SOURCE, _format_chat_time(newest))

    async def _handle_message(self, message: dict[str, Any]) -> tuple[str, str]:
        sender = message.get("sender") or {}
        sender_name = str(sender.get("name") or "")
        if (
            message.get("deleteTime")
            or sender.get("type") == "BOT"
            or sender_name in self.cfg.chat_self_users
        ):
            return "self", ""
        thread = str((message.get("thread") or {}).get("name") or "")
        if message.get("threadReply"):
            return await self._handle_reply(message, thread, sender_name)
        return await self._handle_top_level(message, thread, sender_name)

    async def _handle_top_level(
        self, message: dict[str, Any], thread: str, sender: str
    ) -> tuple[str, str]:
        found = match_allowed_prs(message_search_text(message), self.cfg.repos)
        if not found:
            return "ignored", "no allowed pull-request link"
        claimed = 0
        for pr in found:
            row = await self.store.get(self.tenant_id, pr.repo_full, pr.number)
            if row is None:
                row = self._new_row(pr.repo_full, pr.number, "chat")
                self.summary["discovered"] += 1
            elif row.chat_thread:
                continue  # a repost; the original thread stays authoritative
            row.chat_space = self.cfg.chat_space
            row.chat_thread = thread
            row.chat_message = str(message.get("name") or "")
            row.chat_poster = sender
            if not row.last_reviewed_sha and _raise_trigger(row, "initial"):
                self.summary["triggers"] += 1
            await self.store.save(row)
            claimed += 1
        if claimed and self.chat is not None and self.cfg.claim_reaction:
            await self.chat.react(str(message.get("name") or ""), self.cfg.claim_reaction)
        return ("pr", f"claimed {claimed}") if claimed else ("ignored", "already tracked")

    async def _handle_reply(
        self, message: dict[str, Any], thread: str, sender: str
    ) -> tuple[str, str]:
        rows = [
            r
            for r in await self.store.list_by_thread(self.tenant_id, thread)
            if r.chat_poster and r.chat_poster == sender
        ]
        if not rows:
            return "ignored", "not a tracked thread, or not the original poster"
        text = str(message.get("text") or message.get("argumentText") or "")
        intent = classify_rereview(text)
        named = mentioned_numbers(text) & {r.number for r in rows}
        if named and len(rows) > 1:
            rows = [r for r in rows if r.number in named]
        if intent == "other":
            return "reply", "not a re-review request"
        trigger = "rereview" if intent == "rereview" else "ambiguous"
        raised = 0
        for row in rows:
            if _raise_trigger(row, trigger, text):
                raised += 1
                await self.store.save(row)
        self.summary["triggers"] += raised
        if raised and trigger == "rereview" and self.chat is not None and self.cfg.claim_reaction:
            await self.chat.react(str(message.get("name") or ""), self.cfg.claim_reaction)
        return "reply", f"{trigger} x{raised}"

    async def _say(self, row: PrReviewRow, text: str) -> None:
        if not (self.chat and row.chat_space and row.chat_thread):
            return
        try:
            name = await self.chat.reply(
                row.chat_space, row.chat_thread, f"<{row.url}|#{row.number}>: {text}"
            )
        except Exception as exc:  # noqa: BLE001 - an announcement is not the review
            self._error(f"chat reply {row.key}", exc)
            return
        if name:
            await self.store.record_message(self.tenant_id, name, "self", "intake reply")

    # ── Dispatch ────────────────────────────────────────────────────

    async def _expire_stale(self) -> None:
        cutoff = self.now - timedelta(minutes=self.cfg.stale_after_minutes)
        for row in await self.store.list_active(self.tenant_id):
            if row.updated_at < cutoff:
                row.status = "failed"
                row.error = f"no result within {self.cfg.stale_after_minutes} minutes"
                row.attempts += 1
                row.failed_at = self.now
                await self.store.save(row)
                self.summary["stale"] += 1

    async def _retry_failed(self) -> None:
        """Re-queue failed reviews: a new head at once, the same head after a cooldown."""
        cooldown = timedelta(minutes=self.cfg.retry_cooldown_minutes)
        for row in await self.store.list_failed(self.tenant_id):
            if row.pending_trigger:
                continue
            new_head = bool(row.head_sha and row.queued_sha and row.head_sha != row.queued_sha)
            cooled = row.failed_at is None or row.failed_at <= self.now - cooldown
            if (new_head or (row.attempts < MAX_ATTEMPTS and cooled)) and _raise_trigger(
                row, "retry"
            ):
                await self.store.save(row)
                self.summary["retries"] += 1

    async def _dispatch(self) -> None:
        active = len(await self.store.list_active(self.tenant_id))
        for row in await self.store.list_pending(self.tenant_id):
            if row.status in ACTIVE_STATUSES:
                if not row.followup:
                    row.followup = True
                    await self.store.save(row)
                continue
            if active >= self.cfg.max_concurrent:
                self.summary["deferred"] += 1
                continue
            try:
                if await self._dispatch_one(row):
                    active += 1
            except Exception as exc:  # noqa: BLE001 - keep the trigger; next tick retries
                self._error(f"dispatch {row.key}", exc)
                row.error = str(exc)[:_ERROR_MAX]
                await self.store.save(row)

    @staticmethod
    def _clear(row: PrReviewRow) -> None:
        row.pending_trigger = ""
        row.trigger_text = ""
        row.followup = False

    async def _dispatch_one(self, row: PrReviewRow) -> bool:
        assert self.github is not None
        pr = await self.github.get_pr(row.repo, row.number)
        if pr is None:
            row.status = "failed"
            row.error = "pull request not found"
            self._clear(row)
            await self.store.save(row)
            return False
        if str(pr.get("state") or "") != "open":
            row.status = "closed"
            self._clear(row)
            await self.store.save(row)
            self.summary["closed"] += 1
            merged = "merged" if pr.get("merged") or pr.get("merged_at") else "closed"
            await self._say(row, f"This pull request is {merged}; not reviewing it.")
            return False
        if pr.get("draft"):
            # Stays pending until it is ready for review; say so once.
            if row.error != _DRAFT_NOTE:
                row.error = _DRAFT_NOTE
                await self.store.save(row)
                await self._say(row, "This is a draft; I'll review it once it's ready for review.")
            return False
        head = str((pr.get("head") or {}).get("sha") or "")
        if head != row.queued_sha:
            row.attempts = 0  # a new head starts its retry budget over
        elif trigger_is_retry(row) and row.attempts >= MAX_ATTEMPTS:
            row.error = f"gave up after {row.attempts} failed attempts on this head"
            self._clear(row)
            await self.store.save(row)
            return False
        row.head_sha = head
        row.title = str(pr.get("title") or row.title)
        row.author = str((pr.get("user") or {}).get("login") or row.author)
        row.url = str(pr.get("html_url") or row.url)
        trigger = row.pending_trigger

        kind: Literal["initial", "rereview"] = "rereview" if row.last_reviewed_sha else "initial"
        compare_status = None
        if kind == "rereview" and row.last_reviewed_sha != head:
            compare_status = await self.github.compare_status(row.repo, row.last_reviewed_sha, head)
        decision = decide_review(
            kind=kind,
            head_sha=head,
            last_reviewed_sha=row.last_reviewed_sha or None,
            compare_status=compare_status,
        )
        if decision.action == "skip":
            self._clear(row)
            await self.store.save(row)
            self.summary["no_new_commits"] += 1
            if trigger == "rereview":
                await self._say(row, "No new commits since the last review.")
            return False

        files = await self.github.list_files(row.repo, row.number)
        depth = self.depth_policy.depth_for_pr(pr=pr, files=files)
        if depth is Depth.SKIP:
            row.status = "skipped"
            row.queued_sha = head
            row.depth = depth.value
            self._clear(row)
            await self.store.save(row)
            self.summary["skipped"] += 1
            await self._say(
                row,
                "Skipped: nothing to review here (a bot author, a skip label, or only "
                "lockfiles and generated files changed).",
            )
            return False

        spec = ReviewTaskSpec(
            row=row,
            head_sha=head,
            mode=decision.mode or "full",
            since_sha=decision.since_sha or "",
            depth=depth.value,
            trigger=trigger or kind,
            agent_id=self.cfg.agent_id,
        )
        row.task_id = await self.tasks.create(spec)
        row.status = "queued"
        row.queued_sha = head
        row.mode = spec.mode
        row.depth = spec.depth
        row.error = ""
        self._clear(row)
        await self.store.save(row)
        self.summary["tasks_created"] += 1
        return True

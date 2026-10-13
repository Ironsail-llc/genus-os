"""What the pull-request reviewer is doing and how it went: ``pr_review_status``. No model.

Read-only. It answers from the review rows (``pr_reviews``), the review tasks
(``crm_tasks`` tagged ``pr-review`` — one per review, resolved with
``<VERDICT>: <url>`` or ``<status>: <error>``), the replies the intake gave
(``pr_review_messages``) and the coding jobs in flight.

* **now** — the reviews in flight (how long, the job's status), the queue in
  the order it will run, and the free worker slots.
* **window** (``since``, 24 h by default) — reviews posted by verdict, the
  request-to-answer latency (median and p90), failures and retries, and how
  often the intake answered "No changes?" or "Want a re-review?".
* **problems** — a dropped request (a reviewed pull request whose newer head
  was never reviewed and nothing is queued for it), a stuck review (in flight
  over an hour), a failure out of retries, and the age of the oldest queued
  request. ``alerts`` says each in one line and is empty when all is well —
  what a heartbeat reports on.
* **pr** — one pull request's state and its review history.

**Latency** runs from the request — the Chat message's own time, the poll that
saw a new head, or the operator's ask (the row's ``requested_at``, copied into
the task as ``requested_at:``) — to the moment the review was posted and its
task resolved. A task filed before that line existed counts from its creation.
"""

from __future__ import annotations

import logging
import math
import re
import statistics
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from robothor.pr_review.intake import ASK_REPLY, MAX_ATTEMPTS, NO_CHANGES_REPLY, parse_pr_ref
from robothor.pr_review.store import ACTIVE_STATUSES, FINAL_STATUSES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.pr_review.config import ReviewerConfig
    from robothor.pr_review.store import PrReviewRow, PrReviewStore

    #: ``job_id -> (status, started_at)``, or None when the job is gone.
    JobLookup = Callable[[str], Awaitable[tuple[str, datetime | None] | None]]

__all__ = ["DEFAULT_WINDOW", "MAX_STATE_CHECKS", "QUEUE_ALERT", "STUCK_AFTER", "status_report"]

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = timedelta(hours=24)
#: A review takes 3–8 minutes; one in flight this long is stuck (or the stale
#: limit, when that is lower).
STUCK_AFTER = timedelta(hours=1)
#: The oldest queued request waiting longer than this is worth a word.
QUEUE_ALERT = timedelta(minutes=30)
#: GitHub look-ups of dropped-request candidates per report.
MAX_STATE_CHECKS = 20
_VERDICT_RE = re.compile(r"^(APPROVE|REQUEST_CHANGES|COMMENT):\s")
_FAILURE_RE = re.compile(r"^(?:failed|stale|Prepare failed|Finalize failed):", re.IGNORECASE)
_PR_RE = re.compile(r"^prReview:\s*(\S+?)#(\d+)@", re.MULTILINE)
_LINE_RE = re.compile(r"^(requested_at|trigger):\s*(\S+)\s*$", re.MULTILINE)


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _minutes(delta: timedelta) -> float:
    return round(delta.total_seconds() / 60, 1)


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _task_fields(task: dict[str, Any]) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _LINE_RE.finditer(str(task.get("body") or ""))}


def _task_pr(task: dict[str, Any]) -> str:
    found = _PR_RE.search(str(task.get("body") or ""))
    return f"{found.group(1)}#{found.group(2)}".lower() if found else ""


def _window(
    tasks: list[dict[str, Any]], messages: list[dict[str, Any]], since: datetime
) -> dict[str, Any]:
    posted: dict[str, int] = {}
    latencies: list[float] = []
    failures = retries = 0
    for task in tasks:
        resolved = _time(task.get("resolvedAt"))
        if resolved is None or resolved < since:
            continue
        resolution = str(task.get("resolution") or "")
        fields = _task_fields(task)
        if fields.get("trigger") == "retry":
            retries += 1
        verdict = _VERDICT_RE.match(resolution)
        if verdict:
            posted[verdict.group(1)] = posted.get(verdict.group(1), 0) + 1
            start = _time(fields.get("requested_at")) or _time(task.get("createdAt"))
            if start is not None and start <= resolved:
                latencies.append(_minutes(resolved - start))
        elif _FAILURE_RE.match(resolution):
            failures += 1
    replies = [str(m.get("outcome") or "") for m in messages]
    return {
        "since": since.isoformat(),
        "reviews_posted": sum(posted.values()),
        "posted": posted,
        "latency_minutes": {
            "median": round(statistics.median(latencies), 1) if latencies else None,
            "p90": _p90(latencies) if latencies else None,
            "count": len(latencies),
        },
        "failures": failures,
        "retries": retries,
        "no_changes_replies": sum(1 for r in replies if r.endswith(NO_CHANGES_REPLY)),
        "asked_replies": sum(1 for r in replies if r.endswith(ASK_REPLY)),
    }


async def _in_flight(
    rows: list[PrReviewRow], job_lookup: JobLookup, now: datetime
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.status not in ("reviewing", "posting"):
            continue
        job = await job_lookup(row.job_id) if row.job_id else None
        started = (job[1] if job else None) or row.updated_at
        out.append(
            {
                "pr": row.key,
                "url": row.url,
                "status": row.status,
                "job_id": row.job_id,
                "job_status": job[0] if job else "",
                "running_minutes": _minutes(now - started),
                "head_sha": row.queued_sha or row.head_sha,
            }
        )
    return out


def _queued(rows: list[PrReviewRow], now: datetime) -> list[dict[str, Any]]:
    """Filed tasks first (a worker takes them next), then requests still waiting
    for a task — each in request order, as they will run."""
    filed = [r for r in rows if r.status == "queued"]
    waiting = [
        r
        for r in rows
        if r.pending_trigger and r.status not in ACTIVE_STATUSES and r.status != "closed"
    ]

    def order(r: PrReviewRow) -> tuple[bool, datetime]:
        return (r.requested_at is None, r.requested_at or r.updated_at)

    return [
        {
            "pr": r.key,
            "url": r.url,
            "stage": stage,
            "trigger": r.pending_trigger,
            "waiting_minutes": _minutes(now - (r.requested_at or r.updated_at)),
        }
        for stage, group in (("task filed", filed), ("waiting for a task", waiting))
        for r in sorted(group, key=order)
    ]


async def _dropped(
    store: PrReviewStore, rows: list[PrReviewRow], github: Any | None
) -> list[dict[str, Any]]:
    """Reviewed, a newer head seen since, and nothing queued for it — on an
    open pull request.

    A row keeps its verdict status after its pull request merges, and with
    ``watch_repos`` off nothing polls it again, so each candidate's state is
    checked on GitHub (at most :data:`MAX_STATE_CHECKS` per call). One that is
    no longer open is left out and its row closed, so it is never checked
    again. A candidate that could not be checked (no GitHub, an error, over
    the cap) is listed with ``verified: false`` and raises no alert.
    """
    out: list[dict[str, Any]] = []
    checks = 0
    for r in rows:
        if not (
            r.status in FINAL_STATUSES
            and r.last_reviewed_sha
            and r.head_sha
            and r.head_sha != r.last_reviewed_sha
            and not r.pending_trigger
        ):
            continue
        verified = False
        if github is not None and checks < MAX_STATE_CHECKS:
            checks += 1
            try:
                pr = await github.get_pr(r.repo, r.number)
            except Exception as exc:  # noqa: BLE001 - listed unverified, never alerted
                logger.info("pr_review_status: state of %s unknown: %s", r.key, exc)
            else:
                if pr is None or str(pr.get("state") or "") != "open":
                    r.status = "closed"
                    await store.save(r)
                    continue
                verified = True
        out.append(
            {
                "pr": r.key,
                "url": r.url,
                "reviewed_sha": r.last_reviewed_sha[:12],
                "head_sha": r.head_sha[:12],
                "verified": verified,
            }
        )
    return out


def _one_pr(rows: list[PrReviewRow], tasks: list[dict[str, Any]], pr: str) -> dict[str, Any]:
    parsed = parse_pr_ref(pr)
    if parsed is None:
        return {"error": f"not a pull request: {pr!r} (use a URL or owner/repo#N)"}
    key = f"{parsed[0]}#{parsed[1]}".lower()
    row = next((r for r in rows if r.key.lower() == key), None)
    if row is None:
        return {"error": f"{parsed[0]}#{parsed[1]} is not tracked by the reviewer"}
    history = sorted(
        (
            {
                "created": t.get("createdAt"),
                "resolved": t.get("resolvedAt"),
                "status": t.get("status"),
                "resolution": t.get("resolution") or "",
            }
            for t in tasks
            if _task_pr(t) == key
        ),
        key=lambda h: str(h["created"] or ""),
    )
    return {
        "pr": row.key,
        "url": row.url,
        "title": row.title,
        "author": row.author,
        "status": row.status,
        "head_sha": row.head_sha,
        "last_reviewed_sha": row.last_reviewed_sha,
        "last_verdict": str(row.last_review.get("verdict") or ""),
        "review_url": str(row.last_review.get("url") or ""),
        "pending_trigger": row.pending_trigger,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "job_id": row.job_id,
        "attempts": row.attempts,
        "error": row.error,
        "chat_thread": row.chat_thread,
        "history": history,
    }


async def status_report(
    cfg: ReviewerConfig,
    store: PrReviewStore,
    tenant_id: str,
    *,
    list_tasks: Callable[[], Awaitable[list[dict[str, Any]]]],
    job_lookup: JobLookup,
    now: datetime | None = None,
    since: datetime | None = None,
    pr: str = "",
    github: Any | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = since or now - DEFAULT_WINDOW
    rows = await store.list_all(tenant_id)
    tasks = await list_tasks()
    messages = await store.list_messages(tenant_id, since)
    in_flight = await _in_flight(rows, job_lookup, now)
    queued = _queued(rows, now)
    stuck_after = _minutes(min(STUCK_AFTER, timedelta(minutes=cfg.stale_after_minutes)))
    stuck = [r for r in in_flight if r["running_minutes"] >= stuck_after]
    gave_up = [
        {"pr": r.key, "url": r.url, "attempts": r.attempts, "error": r.error}
        for r in rows
        if r.status == "failed" and r.attempts >= MAX_ATTEMPTS
    ]
    dropped = await _dropped(store, rows, github)
    oldest = max((q["waiting_minutes"] for q in queued), default=0.0)

    alerts = [f"{r['pr']} has been {r['status']} for {r['running_minutes']:.0f} min" for r in stuck]
    alerts += [f"{r['pr']} failed {r['attempts']} times and stopped: {r['error']}" for r in gave_up]
    alerts += [
        f"{r['pr']} has a newer head ({r['head_sha'][:7]}) than its review and nothing queued"
        for r in dropped
        if r["verified"]
    ]
    if oldest > _minutes(QUEUE_ALERT):
        alerts.append(f"the oldest queued review has waited {oldest:.0f} min")

    report: dict[str, Any] = {
        "now": {
            "in_flight": in_flight,
            "queued": queued,
            "free_slots": max(0, cfg.workers - len(in_flight)),
            "workers": cfg.workers,
        },
        "window": _window(tasks, messages, since),
        "problems": {
            "stuck": stuck,
            "gave_up": gave_up,
            "dropped_requests": dropped,
            "queue_oldest_minutes": oldest,
        },
        "alerts": alerts,
    }
    if pr:
        report["pr"] = _one_pr(rows, tasks, pr)
    return report

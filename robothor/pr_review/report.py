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
* **paired** (only with ``peer_logins``) — for each head reviewed in the
  window (newest :data:`MAX_PAIRED_HEADS`), each peer's verdict at that
  commit, whether it agrees, and the findings only we, only the peer, or both
  raised (same file, lines within :data:`MATCH_LINES`); a per-peer summary;
  and alerts for an approve-versus-request-changes split or a peer silent
  past the stale window.

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

__all__ = [
    "DEFAULT_WINDOW",
    "MATCH_LINES",
    "MAX_PAIRED_HEADS",
    "MAX_STATE_CHECKS",
    "QUEUE_ALERT",
    "STUCK_AFTER",
    "status_report",
]

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


#: How many posted reviews the window lists; the rest are counted, not listed.
REVIEW_LIST_LIMIT = 50


def _window(
    tasks: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    since: datetime,
    titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    posted: dict[str, int] = {}
    reviews: list[dict[str, Any]] = []
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
            pr = _task_pr(task)
            reviews.append(
                {
                    "pr": pr,
                    "title": (titles or {}).get(pr, ""),
                    "verdict": verdict.group(1),
                    "url": resolution[verdict.end() :].strip(),
                    "at": resolved.isoformat(),
                }
            )
            start = _time(fields.get("requested_at")) or _time(task.get("createdAt"))
            if start is not None and start <= resolved:
                latencies.append(_minutes(resolved - start))
        elif _FAILURE_RE.match(resolution):
            failures += 1
    replies = [str(m.get("outcome") or "") for m in messages]
    reviews.sort(key=lambda rv: rv["at"], reverse=True)
    return {
        "since": since.isoformat(),
        "reviews_posted": sum(posted.values()),
        "posted": posted,
        # Which pull requests, so "what did you review today?" is answerable
        # from here on request — the per-review Telegram line is off by default.
        "reviews": reviews[:REVIEW_LIST_LIMIT],
        "reviews_not_listed": max(0, len(reviews) - REVIEW_LIST_LIMIT),
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


#: Pull-request heads compared with paired reviewers per report (two GitHub
#: calls each); the rest are counted in ``heads_not_compared``.
MAX_PAIRED_HEADS = 10
#: Two findings on the same file this many lines apart or closer are the same.
MATCH_LINES = 3
#: Our verdict in the words GitHub uses for a review's state.
_GITHUB_STATE = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}
_VERDICT_STATES = frozenset(_GITHUB_STATE.values())


def _login(item: dict[str, Any]) -> str:
    return str((item.get("user") or {}).get("login") or "").lower()


def _matches(ours: dict[str, Any], theirs: dict[str, Any]) -> bool:
    if not ours.get("path") or ours.get("path") != theirs.get("path"):
        return False
    a, b = ours.get("line"), theirs.get("line") or theirs.get("original_line")
    if isinstance(a, int) and isinstance(b, int):
        return abs(a - b) <= MATCH_LINES
    return a is None and b is None


def _compare_findings(ours: list[dict[str, Any]], theirs: list[dict[str, Any]]) -> dict[str, int]:
    """One-to-one: each of ours pairs with at most one of theirs."""
    left = list(theirs)
    both = 0
    for issue in ours:
        hit = next((t for t in left if _matches(issue, t)), None)
        if hit is not None:
            left.remove(hit)
            both += 1
    return {"only_ours": len(ours) - both, "only_peer": len(left), "both": both}


def _peer_at_head(
    peer: str,
    sha: str,
    ours: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
) -> dict[str, Any]:
    """The peer's latest verdict at ``sha`` and how its inline findings compare."""
    mine = [
        r
        for r in reviews
        if _login(r) == peer
        and str(r.get("commit_id") or "") == sha
        and str(r.get("state") or "") in _VERDICT_STATES
    ]
    if not mine:
        return {"verdict": None, "agree": None, "only_ours": None, "only_peer": None, "both": None}
    ids = {r.get("id") for r in mine}
    theirs = [
        c
        for c in comments
        if _login(c) == peer
        and c.get("pull_request_review_id") in ids
        and not c.get("in_reply_to_id")
    ]
    return {"verdict": str(mine[-1]["state"]), "agree": None, **_compare_findings(ours, theirs)}


async def _paired(
    cfg: ReviewerConfig,
    rows: list[PrReviewRow],
    reviewed_at: dict[str, datetime],
    github: Any | None,
    now: datetime,
) -> tuple[dict[str, Any], list[str]]:
    """This reviewer against each peer on every head it reviewed in the window.

    ``reviewed_at`` maps a pull request (lowercased key) to when its latest
    review in the window was posted; the row's ``last_review`` is that review.
    A failed lookup leaves the peer's verdict ``"unknown"`` and raises no alert.
    """
    candidates = sorted(
        (r for r in rows if r.key.lower() in reviewed_at and str(r.last_review.get("sha") or "")),
        key=lambda r: reviewed_at[r.key.lower()],
        reverse=True,
    )
    stale = timedelta(minutes=cfg.stale_after_minutes)
    heads: list[dict[str, Any]] = []
    alerts: list[str] = []
    totals = {
        peer: {"heads_compared": 0, "agreed": 0, "only_ours": 0, "only_peer": 0, "both": 0}
        for peer in cfg.peer_logins
    }
    for row in candidates[:MAX_PAIRED_HEADS]:
        sha = str(row.last_review["sha"])
        ours_verdict = _GITHUB_STATE.get(str(row.last_review.get("verdict") or ""), "")
        ours = [i for i in row.last_review.get("issues") or [] if isinstance(i, dict)]
        at = reviewed_at[row.key.lower()]
        fetched: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None
        if github is not None:
            try:
                reviews = list(await github.list_reviews(row.repo, row.number) or [])
                comments = (
                    list(await github.list_review_comments(row.repo, row.number) or [])
                    if any(_login(r) in cfg.peer_logins for r in reviews)
                    else []
                )
                fetched = (reviews, comments)
            except Exception as exc:  # noqa: BLE001 - the peer is unknown, never alerted
                logger.info("pr_review_status: peer reviews of %s unknown: %s", row.key, exc)
        peers: dict[str, Any] = {}
        for peer in cfg.peer_logins:
            if fetched is None:
                peers[peer] = {
                    "verdict": "unknown",
                    "agree": None,
                    "only_ours": None,
                    "only_peer": None,
                    "both": None,
                }
                continue
            entry = _peer_at_head(peer, sha, ours, *fetched)
            if entry["verdict"] is None:
                if now - at > stale:
                    alerts.append(
                        f"{peer} has posted nothing on {row.key} @{sha[:7]}, "
                        f"reviewed {_minutes(now - at):.0f} min ago"
                    )
            else:
                entry["agree"] = entry["verdict"] == ours_verdict
                total = totals[peer]
                total["heads_compared"] += 1
                total["agreed"] += int(entry["agree"])
                for k in ("only_ours", "only_peer", "both"):
                    total[k] += entry[k]
                if {entry["verdict"], ours_verdict} == {"APPROVED", "CHANGES_REQUESTED"}:
                    alerts.append(
                        f"{row.key} @{sha[:7]}: we {ours_verdict.lower().replace('_', ' ')}, "
                        f"{peer} {entry['verdict'].lower().replace('_', ' ')}"
                    )
            peers[peer] = entry
        heads.append(
            {
                "pr": row.key,
                "url": row.url,
                "head_sha": sha[:12],
                "reviewed_at": at.isoformat(),
                "verdict": ours_verdict,
                "peers": peers,
            }
        )
    summary = {
        peer: {
            "heads_compared": t["heads_compared"],
            "agreement_rate": (
                round(t["agreed"] / t["heads_compared"], 2) if t["heads_compared"] else None
            ),
            "only_ours": t["only_ours"],
            "only_peer": t["only_peer"],
            "both": t["both"],
        }
        for peer, t in totals.items()
    }
    return (
        {
            "peers": list(cfg.peer_logins),
            "heads": heads,
            "heads_not_compared": max(0, len(candidates) - MAX_PAIRED_HEADS),
            "summary": summary,
        },
        alerts,
    )


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

    window = _window(tasks, messages, since, {r.key.lower(): r.title for r in rows})
    paired: dict[str, Any] | None = None
    if cfg.peer_logins:
        reviewed_at: dict[str, datetime] = {}
        for rv in window["reviews"]:  # newest first: the first seen is the latest
            at = _time(rv["at"])
            if rv["pr"] and at is not None:
                reviewed_at.setdefault(rv["pr"], at)
        paired, paired_alerts = await _paired(cfg, rows, reviewed_at, github, now)
        alerts += paired_alerts

    report: dict[str, Any] = {
        "now": {
            "in_flight": in_flight,
            "queued": queued,
            "free_slots": max(0, cfg.workers - len(in_flight)),
            "workers": cfg.workers,
        },
        "window": window,
        "problems": {
            "stuck": stuck,
            "gave_up": gave_up,
            "dropped_requests": dropped,
            "queue_oldest_minutes": oldest,
        },
        "alerts": alerts,
    }
    if paired is not None:
        report["paired"] = paired
    if pr:
        report["pr"] = _one_pr(rows, tasks, pr)
    return report

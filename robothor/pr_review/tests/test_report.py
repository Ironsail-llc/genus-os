"""pr_review_status: what the reviewer is doing now, and how the window went. No model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from robothor.pr_review.config import ReviewerConfig
from robothor.pr_review.report import status_report
from robothor.pr_review.store import MemoryStore, PrReviewRow
from robothor.pr_review.tests.fakes import REPO, FakeGitHub, make_pr

TENANT = "test-tenant"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
CFG = ReviewerConfig(repos=(REPO,), workers=3, stale_after_minutes=180)


def _task(
    n: int,
    *,
    created: datetime,
    resolved: datetime | None = None,
    resolution: str = "",
    requested: datetime | None = None,
    trigger: str = "rereview",
) -> dict:
    body = f"repo: {REPO}\nnumber: {n}\ntrigger: {trigger}\n"
    if requested:
        body += f"requested_at: {requested.isoformat()}\n"
    body += f"\nprReview: {REPO}#{n}@{'a' * 40}\n"
    return {
        "id": f"task-{n}-{created.minute}",
        "title": f"pr-review {REPO}#{n} @aaaaaaa",
        "body": body,
        "status": "DONE" if resolved else "TODO",
        "createdAt": created.isoformat(),
        "resolvedAt": resolved.isoformat() if resolved else None,
        "resolution": resolution,
    }


async def _row(store, n, **kw) -> PrReviewRow:
    row = PrReviewRow(
        tenant_id=TENANT, repo=REPO, number=n, url=f"https://github.com/{REPO}/pull/{n}", **kw
    )
    await store.save(row)
    stored = store.rows[(TENANT, REPO, n)]
    if "updated_at" in kw:
        stored.updated_at = kw["updated_at"]
    return row


async def _report(store, tasks=(), jobs=None, **kw):
    async def job_lookup(job_id):
        return (jobs or {}).get(job_id)

    async def list_tasks():
        return list(tasks)

    return await status_report(
        CFG, store, TENANT, list_tasks=list_tasks, job_lookup=job_lookup, now=NOW, **kw
    )


async def test_window_counts_verdicts_and_latency_from_the_request():
    store = MemoryStore()
    tasks = []
    # Five posted reviews: requested 10, 20, 30, 40 and 100 minutes before posting.
    for n, wait in enumerate((10, 20, 30, 40, 100), start=1):
        posted = NOW - timedelta(hours=1)
        verdict = "APPROVE" if n % 2 else "REQUEST_CHANGES"
        tasks.append(
            _task(
                n,
                created=posted - timedelta(minutes=5),
                resolved=posted,
                resolution=f"{verdict}: https://github.com/{REPO}/pull/{n}#r",
                requested=posted - timedelta(minutes=wait),
            )
        )
    tasks.append(
        _task(
            9,
            created=NOW - timedelta(hours=2),
            resolved=NOW - timedelta(hours=2),
            resolution="failed: review job failed: boom",
            trigger="retry",
        )
    )
    # Outside the window: not counted.
    tasks.append(
        _task(
            8,
            created=NOW - timedelta(days=3),
            resolved=NOW - timedelta(days=3),
            resolution="APPROVE: x",
        )
    )
    report = await _report(store, tasks)
    window = report["window"]
    assert window["posted"] == {"APPROVE": 3, "REQUEST_CHANGES": 2}
    assert window["reviews_posted"] == 5
    assert window["latency_minutes"] == {"median": 30.0, "p90": 100.0, "count": 5}
    assert window["failures"] == 1 and window["retries"] == 1


async def test_latency_falls_back_to_the_task_time_for_older_tasks():
    tasks = [
        _task(
            1,
            created=NOW - timedelta(minutes=50),
            resolved=NOW - timedelta(minutes=20),
            resolution="APPROVE: u",
        )
    ]
    report = await _report(MemoryStore(), tasks)
    assert report["window"]["latency_minutes"]["median"] == 30.0


async def test_now_shows_what_runs_what_waits_in_order_and_free_slots():
    store = MemoryStore()
    await _row(store, 1, status="reviewing", job_id="job-1", head_sha="1" * 40)
    await _row(store, 2, status="queued", requested_at=NOW - timedelta(minutes=12))
    await _row(
        store,
        3,
        status="approved",
        pending_trigger="rereview",
        requested_at=NOW - timedelta(minutes=5),
    )
    await _row(store, 4, status="queued", requested_at=NOW - timedelta(minutes=20))
    jobs = {"job-1": ("running", NOW - timedelta(minutes=7))}
    report = await _report(store, jobs=jobs)
    now = report["now"]
    assert [r["pr"] for r in now["in_flight"]] == [f"{REPO}#1"]
    assert now["in_flight"][0]["job_status"] == "running"
    assert now["in_flight"][0]["running_minutes"] == 7.0
    assert [r["pr"] for r in now["queued"]] == [f"{REPO}#4", f"{REPO}#2", f"{REPO}#3"]
    assert now["queued"][0]["waiting_minutes"] == 20.0
    assert now["free_slots"] == 2
    assert report["alerts"] == []


async def test_a_dropped_request_is_flagged():
    store = MemoryStore()
    # Reviewed at SHA1, a newer head was seen, and nothing is queued for it.
    await _row(
        store,
        5,
        status="changes_requested",
        head_sha="2" * 40,
        last_reviewed_sha="1" * 40,
        chat_thread="spaces/S/threads/t",
    )
    # Same, but its request is queued: not dropped.
    await _row(
        store,
        6,
        status="approved",
        head_sha="2" * 40,
        last_reviewed_sha="1" * 40,
        pending_trigger="rereview",
        requested_at=NOW,
    )
    # Reviewed at its head: fine.
    await _row(store, 7, status="approved", head_sha="1" * 40, last_reviewed_sha="1" * 40)
    github = FakeGitHub()
    github.add(make_pr(5, "2" * 40))
    report = await _report(store, github=github)
    assert [d["pr"] for d in report["problems"]["dropped_requests"]] == [f"{REPO}#5"]
    assert report["problems"]["dropped_requests"][0]["verified"] is True
    assert any("#5" in a for a in report["alerts"])


# Live QA on #677: rows whose newer head was never reviewed keep their verdict
# status after the pull request merges, and nothing polls them again (watch
# repos off) — the heartbeat would have alerted on four merged PRs forever.


async def _dropped_row(store, n):
    await _row(store, n, status="approved", head_sha="2" * 40, last_reviewed_sha="1" * 40)


async def test_a_merged_or_closed_candidate_is_excluded_and_its_row_closed():
    store = MemoryStore()
    for n in (5, 6, 7):
        await _dropped_row(store, n)
    github = FakeGitHub()
    github.add(make_pr(5, "2" * 40))  # still open
    github.add(make_pr(6, "2" * 40, state="closed"))  # merged or closed
    # 7: gone from GitHub
    report = await _report(store, github=github)
    assert [d["pr"] for d in report["problems"]["dropped_requests"]] == [f"{REPO}#5"]
    assert not any("#6" in a or "#7" in a for a in report["alerts"])
    assert (await store.get(TENANT, REPO, 6)).status == "closed"
    assert (await store.get(TENANT, REPO, 7)).status == "closed"
    assert (await store.get(TENANT, REPO, 5)).status == "approved"
    # Closed rows are never checked again.
    again = await _report(store, github=github)
    assert [d["pr"] for d in again["problems"]["dropped_requests"]] == [f"{REPO}#5"]


async def test_without_github_candidates_are_unverified_and_raise_no_alert():
    store = MemoryStore()
    await _dropped_row(store, 5)
    report = await _report(store)
    [dropped] = report["problems"]["dropped_requests"]
    assert dropped["pr"] == f"{REPO}#5" and dropped["verified"] is False
    assert report["alerts"] == []
    assert (await store.get(TENANT, REPO, 5)).status == "approved"


async def test_a_failing_github_check_leaves_the_candidate_unverified():
    store = MemoryStore()
    await _dropped_row(store, 5)
    github = FakeGitHub()

    async def boom(repo, number):
        raise RuntimeError("502")

    github.get_pr = boom  # type: ignore[method-assign]
    report = await _report(store, github=github)
    assert report["problems"]["dropped_requests"][0]["verified"] is False
    assert report["alerts"] == []


async def test_github_checks_are_capped_per_call():
    from robothor.pr_review.report import MAX_STATE_CHECKS

    store = MemoryStore()
    total = MAX_STATE_CHECKS + 5
    github = FakeGitHub()
    for n in range(1, total + 1):
        await _dropped_row(store, n)
        github.add(make_pr(n, "2" * 40))
    calls = 0
    real = github.get_pr

    async def counting(repo, number):
        nonlocal calls
        calls += 1
        return await real(repo, number)

    github.get_pr = counting  # type: ignore[method-assign]
    report = await _report(store, github=github)
    assert calls == MAX_STATE_CHECKS
    dropped = report["problems"]["dropped_requests"]
    assert len(dropped) == total
    assert sum(1 for d in dropped if d["verified"]) == MAX_STATE_CHECKS
    assert len([a for a in report["alerts"] if "newer head" in a]) == MAX_STATE_CHECKS


async def test_stuck_reviews_retries_run_out_and_an_old_queue_raise_alerts():
    store = MemoryStore()
    await _row(store, 1, status="posting", job_id="job-1", updated_at=NOW - timedelta(hours=2))
    await _row(store, 2, status="failed", attempts=3, error="review job failed")
    await _row(store, 3, status="queued", requested_at=NOW - timedelta(minutes=45))
    report = await _report(store, jobs={"job-1": ("done", NOW - timedelta(hours=2))})
    problems = report["problems"]
    assert [r["pr"] for r in problems["stuck"]] == [f"{REPO}#1"]
    assert [r["pr"] for r in problems["gave_up"]] == [f"{REPO}#2"]
    assert problems["queue_oldest_minutes"] == 45.0
    assert len(report["alerts"]) == 3


async def test_replies_given_are_counted_from_the_recorded_messages():
    store = MemoryStore()
    await store.record_message(TENANT, "m1", "self", "intake reply: No changes?")
    await store.record_message(TENANT, "m2", "self", "intake reply: No changes?")
    await store.record_message(
        TENANT, "m3", "self", 'intake reply: Want a re-review? Reply "re-review".'
    )
    await store.record_message(TENANT, "m4", "self", "finalize announce")
    report = await _report(store)
    assert report["window"]["no_changes_replies"] == 2
    assert report["window"]["asked_replies"] == 1


async def test_one_pr_shows_its_state_and_history():
    store = MemoryStore()
    await _row(
        store,
        7,
        status="approved",
        head_sha="1" * 40,
        last_reviewed_sha="1" * 40,
        last_review={"verdict": "APPROVE", "url": "https://example.com/r"},
    )
    tasks = [
        _task(
            7,
            created=NOW - timedelta(hours=3),
            resolved=NOW - timedelta(hours=2, minutes=50),
            resolution="REQUEST_CHANGES: u1",
        ),
        _task(
            7,
            created=NOW - timedelta(hours=1),
            resolved=NOW - timedelta(minutes=50),
            resolution="APPROVE: u2",
        ),
        _task(8, created=NOW - timedelta(hours=1), resolved=NOW, resolution="APPROVE: u3"),
    ]
    report = await _report(store, tasks, pr=f"{REPO}#7")
    one = report["pr"]
    assert one["status"] == "approved" and one["last_verdict"] == "APPROVE"
    assert one["review_url"] == "https://example.com/r"
    assert [h["resolution"] for h in one["history"]] == ["REQUEST_CHANGES: u1", "APPROVE: u2"]


async def test_an_unknown_pr_says_so():
    report = await _report(MemoryStore(), pr=f"{REPO}#99")
    assert "not tracked" in report["pr"]["error"]


async def test_window_lists_each_posted_review_newest_first_with_its_link():
    store = MemoryStore()
    await _row(store, 1, title="Fix the billing sync")
    tasks = [
        _task(
            1,
            created=NOW - timedelta(hours=3),
            resolved=NOW - timedelta(hours=3),
            resolution=f"REQUEST_CHANGES: https://github.com/{REPO}/pull/1#r1",
        ),
        _task(
            1,
            created=NOW - timedelta(hours=1),
            resolved=NOW - timedelta(hours=1),
            resolution=f"APPROVE: https://github.com/{REPO}/pull/1#r2",
        ),
        # A failure is not a posted review, and one outside the window is left out.
        _task(2, created=NOW, resolved=NOW, resolution="failed: boom"),
        _task(
            3,
            created=NOW - timedelta(days=3),
            resolved=NOW - timedelta(days=3),
            resolution="APPROVE: https://example.com/old",
        ),
    ]
    reviews = (await _report(store, tasks))["window"]["reviews"]
    assert reviews == [
        {
            "pr": f"{REPO}#1".lower(),
            "title": "Fix the billing sync",
            "verdict": "APPROVE",
            "url": f"https://github.com/{REPO}/pull/1#r2",
            "at": (NOW - timedelta(hours=1)).isoformat(),
        },
        {
            "pr": f"{REPO}#1".lower(),
            "title": "Fix the billing sync",
            "verdict": "REQUEST_CHANGES",
            "url": f"https://github.com/{REPO}/pull/1#r1",
            "at": (NOW - timedelta(hours=3)).isoformat(),
        },
    ]


async def test_window_review_list_is_capped_and_says_how_many_it_left_out():
    tasks = [
        _task(
            n,
            created=NOW - timedelta(minutes=n),
            resolved=NOW - timedelta(minutes=n),
            resolution=f"APPROVE: https://example.com/{n}",
        )
        for n in range(1, 61)
    ]
    window = (await _report(MemoryStore(), tasks))["window"]
    assert len(window["reviews"]) == 50
    assert window["reviews"][0]["url"] == "https://example.com/1"
    assert window["reviews_not_listed"] == 10

"""The deterministic intake against fake GitHub, fake Chat and a fake task sink."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.pr_review.config import ReviewerConfig
from robothor.pr_review.intake import Intake, parse_chat_time
from robothor.pr_review.store import MemoryStore
from robothor.pr_review.tests.fakes import REPO, FakeChat, FakeGitHub, FakeTasks, make_pr

TENANT = "test-tenant"
NOW = datetime(2026, 10, 5, 10, 30, tzinfo=UTC)
SHA1 = "1" * 40
SHA2 = "2" * 40
URL = f"https://github.com/{REPO}/pull/7"


@pytest.fixture
def env():
    return {
        "store": MemoryStore(),
        "github": FakeGitHub(),
        "chat": FakeChat(),
        "tasks": FakeTasks(),
    }


def _cfg(**kw):
    base = {"repos": (REPO,), "watch_repos": False, "chat_space": "spaces/AAAA"}
    base.update(kw)
    return ReviewerConfig(**base)


async def _run(env, cfg=None, now=NOW, **kw):
    intake = Intake(
        cfg or _cfg(),
        env["store"],
        TENANT,
        tasks=env["tasks"],
        github=env["github"],
        chat=env["chat"],
        now=now,
        **kw,
    )
    return await intake.run()


async def _finish(env, number=7, sha=SHA1, status="changes_requested"):
    """What pr_review_finalize does to the row after posting."""
    row = await env["store"].get(TENANT, REPO, number)
    row.status = status
    row.last_reviewed_sha = sha
    row.review_ids = [*row.review_ids, 100 + len(row.review_ids)]
    await env["store"].save(row)


async def test_top_level_link_claims_and_creates_one_task(env):
    env["github"].add(make_pr(7, SHA1))
    msg = env["chat"].post(f"please review {URL}")
    summary = await _run(env)
    assert summary["tasks_created"] == 1
    assert env["chat"].reactions == [(msg["name"], "\U0001f440")]
    task = env["tasks"].created[0]
    assert task["title"] == f"pr-review {REPO}#7 @{SHA1[:7]}"
    body = task["body"]
    for line in (
        f"repo: {REPO}",
        "number: 7",
        f"head_sha: {SHA1}",
        "last_reviewed_sha: none",
        "mode: full",
        "depth: full",
        "chat_thread: spaces/AAAA/threads/t1",
        f"prReview: {REPO}#7@{SHA1}",
    ):
        assert line in body
    row = await env["store"].get(TENANT, REPO, 7)
    assert row.status == "queued" and row.queued_sha == SHA1
    assert row.chat_poster == "users/alice"


async def test_links_to_other_repos_are_ignored(env):
    env["chat"].post("see https://github.com/other/repo/pull/1")
    summary = await _run(env)
    assert summary["tasks_created"] == 0
    assert env["chat"].reactions == []


async def test_second_tick_does_not_duplicate_the_task(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    await _run(env)
    summary = await _run(env)
    assert summary["tasks_created"] == 0
    assert len(env["tasks"].created) == 1


async def test_rereview_from_the_poster_with_a_new_sha_queues_incremental(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    await _run(env)
    await _finish(env)
    env["github"].add(make_pr(7, SHA2))
    reply = env["chat"].post("fixed, ptal", reply=True, time="2026-10-05T10:05:00.000000Z")
    summary = await _run(env)
    assert summary["tasks_created"] == 1
    task = env["tasks"].created[-1]
    assert "mode: incremental" in task["body"]
    assert f"since_sha: {SHA1}" in task["body"]
    assert f"last_reviewed_sha: {SHA1}" in task["body"]
    assert (reply["name"], "\U0001f440") in env["chat"].reactions


async def test_rereview_from_someone_else_is_ignored(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    await _run(env)
    await _finish(env)
    env["github"].add(make_pr(7, SHA2))
    env["chat"].post("ptal", reply=True, sender="users/bob", time="2026-10-05T10:05:00.000000Z")
    summary = await _run(env)
    assert summary["tasks_created"] == 0


async def test_negated_reply_is_handed_to_the_agent_as_ambiguous(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    await _run(env)
    await _finish(env)
    env["github"].add(make_pr(7, SHA2))
    env["chat"].post("not fixed yet", reply=True, time="2026-10-05T10:05:00.000000Z")
    await _run(env)
    task = env["tasks"].created[-1]
    assert "trigger: ambiguous" in task["body"]
    assert "<reply>not fixed yet</reply>" in task["body"]


async def test_acknowledgement_reply_triggers_nothing(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    await _run(env)
    await _finish(env)
    env["github"].add(make_pr(7, SHA2))
    env["chat"].post("thanks!", reply=True, time="2026-10-05T10:05:00.000000Z")
    summary = await _run(env)
    assert summary["tasks_created"] == 0


async def test_rereview_without_new_commits_says_so(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    await _run(env)
    await _finish(env)
    env["chat"].post("ptal", reply=True, time="2026-10-05T10:05:00.000000Z")
    summary = await _run(env)
    assert summary["tasks_created"] == 0
    assert summary["no_new_commits"] == 1
    assert env["chat"].replies[-1][2] == f"<{URL}|#7>: No new commits since the last review."


async def test_malformed_message_is_recorded_and_skipped(env):
    env["github"].add(make_pr(7, SHA1))
    bad = env["chat"].post("x")
    bad["thread"] = "not-a-dict"  # .get on a str raises inside the handler
    env["chat"].post(f"review {URL}", time="2026-10-05T10:01:00.000000Z")
    summary = await _run(env)
    assert summary["chat_errors"] == 1
    assert summary["tasks_created"] == 1
    assert env["store"].messages[(TENANT, bad["name"])]["kind"] == "error"
    # The next tick does not retry it.
    summary = await _run(env)
    assert summary["chat_errors"] == 0


async def test_cursor_persists_and_moves_forward(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}", time="2026-10-05T10:01:00.123456789Z")
    await _run(env)
    cursor = await env["store"].get_cursor(TENANT, "chat")
    assert cursor == "2026-10-05T10:01:00.123456Z"
    await _run(env)
    # Second poll starts 60 s before the stored cursor, not from the lookback.
    assert env["chat"].list_calls[-1] == "2026-10-05T10:00:00.123456Z"
    assert env["chat"].list_calls[0] == "2026-10-05T09:30:00.000000Z"


async def test_bot_and_self_messages_are_ignored(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}", sender="users/robot")
    summary = await _run(env, _cfg(chat_self_users=("users/robot",)))
    assert summary["tasks_created"] == 0


async def test_watched_repo_new_sha_reenqueues_once(env):
    env["github"].add(make_pr(7, SHA1))
    cfg = _cfg(watch_repos=True, chat_space="")
    assert (await _run(env, cfg))["tasks_created"] == 1
    await _finish(env)
    assert (await _run(env, cfg))["tasks_created"] == 0  # same head, already reviewed
    env["github"].add(make_pr(7, SHA2))
    assert (await _run(env, cfg))["tasks_created"] == 1
    assert (await _run(env, cfg))["tasks_created"] == 0  # task open for SHA2
    assert [t["spec"].head_sha for t in env["tasks"].created] == [SHA1, SHA2]


async def test_new_sha_during_a_review_waits_for_finalize(env):
    env["github"].add(make_pr(7, SHA1))
    cfg = _cfg(watch_repos=True, chat_space="")
    await _run(env, cfg)
    env["github"].add(make_pr(7, SHA2))
    summary = await _run(env, cfg)
    assert summary["tasks_created"] == 0
    row = await env["store"].get(TENANT, REPO, 7)
    assert row.followup is True and row.pending_trigger  # kept for after finalize
    await _finish(env, sha=SHA1)
    assert (await _run(env, cfg))["tasks_created"] == 1


async def test_drafts_and_closed_prs_are_not_reviewed(env):
    env["github"].add(make_pr(7, SHA1, draft=True))
    env["github"].add(make_pr(8, SHA2, state="closed"))
    summary = await _run(env, _cfg(watch_repos=True, chat_space=""))
    assert summary["tasks_created"] == 0


async def test_lockfile_only_change_is_skipped(env):
    env["github"].add(
        make_pr(7, SHA1),
        files=[{"filename": "package-lock.json", "additions": 900, "deletions": 3}],
    )
    summary = await _run(env, _cfg(watch_repos=True, chat_space=""))
    assert summary["skipped"] == 1 and summary["tasks_created"] == 0
    summary = await _run(env, _cfg(watch_repos=True, chat_space=""))
    assert summary["skipped"] == 0  # not re-evaluated at the same head


async def test_max_concurrent_defers_the_rest(env):
    for n in (1, 2, 3):
        env["github"].add(make_pr(n, str(n) * 40))
    summary = await _run(env, _cfg(watch_repos=True, chat_space="", max_concurrent=2))
    assert summary["tasks_created"] == 2 and summary["deferred"] == 1


async def test_review_requested_only_from_configured_repos(env):
    # A review request from a fork or any repository the token can read is not
    # an invitation to clone it and run Claude Code over it.
    env["github"].add(make_pr(5, SHA1, repo="acme/gadgets"), repo="acme/gadgets")
    env["github"].add(make_pr(6, SHA2))
    env["github"].requested = [("acme/gadgets", 5), (REPO, 6)]
    summary = await _run(env, _cfg(bot_login="review-bot", chat_space=""))
    assert summary["tasks_created"] == 1
    assert env["tasks"].created[0]["spec"].row.number == 6


async def test_one_failing_repo_does_not_stop_the_others(env):
    env["github"].add(make_pr(5, SHA1, repo="acme/gadgets"), repo="acme/gadgets")
    env["github"].fail_list = {REPO}
    cfg = _cfg(repos=(REPO, "acme/gadgets"), watch_repos=True, chat_space="")
    summary = await _run(env, cfg)
    assert summary["tasks_created"] == 1
    assert summary["errors"] and "boom" in summary["errors"][0]


async def test_stale_review_is_failed_and_frees_the_slot(env):
    env["github"].add(make_pr(7, SHA1))
    cfg = _cfg(watch_repos=True, chat_space="", stale_after_minutes=30)
    await _run(env, cfg)
    later = datetime.now(UTC) + timedelta(hours=2)
    summary = await _run(env, cfg, now=later)
    assert summary["stale"] == 1
    row = await env["store"].get(TENANT, REPO, 7)
    assert row.status == "failed"


async def test_not_configured_runs_nothing(env):
    summary = await _run(env, ReviewerConfig())
    assert summary["tasks_created"] == 0 and summary["errors"] == []


def test_parse_chat_time_accepts_nanoseconds():
    assert parse_chat_time("2026-10-05T10:01:00.123456789Z") == datetime(
        2026, 10, 5, 10, 1, 0, 123456, tzinfo=UTC
    )
    assert parse_chat_time("") is None
    assert parse_chat_time("garbage") is None


# ── serialization ───────────────────────────────────────────────────────


async def test_a_concurrent_intake_run_is_skipped(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    async with env["store"].intake_lock(TENANT) as held:
        assert held
        summary = await _run(env)
    assert summary == {"skipped": "locked"}
    assert env["tasks"].created == []


async def test_count_only_never_dispatches(env):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    intake = Intake(
        _cfg(),
        env["store"],
        TENANT,
        tasks=env["tasks"],
        github=env["github"],
        chat=env["chat"],
        now=NOW,
    )
    await intake._poll_chat()  # a trigger is pending, nothing dispatched yet
    summary = await Intake(
        _cfg(),
        env["store"],
        TENANT,
        tasks=env["tasks"],
        github=env["github"],
        chat=env["chat"],
        now=NOW,
    ).run(count_only=True)
    assert env["tasks"].created == []
    assert summary["queued_tasks"] == 0 and summary["pending"] == 1


async def test_a_trigger_raised_during_posting_survives_the_post(env):
    # The intake and finalize write one row; neither may revert the other's columns.
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    await _run(env)
    reviewer_copy = await env["store"].get(TENANT, REPO, 7)
    intake_copy = await env["store"].get(TENANT, REPO, 7)
    intake_copy.pending_trigger = "rereview"
    await env["store"].save(intake_copy)
    reviewer_copy.status = "commented"
    reviewer_copy.last_reviewed_sha = SHA1
    await env["store"].save(reviewer_copy)
    row = await env["store"].get(TENANT, REPO, 7)
    assert (row.pending_trigger, row.status) == ("rereview", "commented")


# ── say why, retry failures ─────────────────────────────────────────────


async def test_a_closed_chat_pr_gets_a_reply_saying_so(env):
    env["github"].add(make_pr(7, SHA1, state="closed"))
    env["chat"].post(f"review {URL}")
    await _run(env)
    assert env["chat"].replies and "closed" in env["chat"].replies[-1][2].lower()


async def test_a_draft_chat_pr_is_told_once(env):
    env["github"].add(make_pr(7, SHA1, draft=True))
    env["chat"].post(f"review {URL}")
    await _run(env)
    await _run(env)
    assert len(env["chat"].replies) == 1 and "draft" in env["chat"].replies[0][2].lower()


async def test_a_skipped_chat_pr_gets_a_reply_saying_why(env):
    env["github"].add(
        make_pr(7, SHA1), files=[{"filename": "yarn.lock", "additions": 900, "deletions": 3}]
    )
    env["chat"].post(f"review {URL}")
    await _run(env)
    assert env["chat"].replies and "skip" in env["chat"].replies[-1][2].lower()


async def _fail_review(env, sha=SHA1, attempts=1, minutes_ago=0):
    row = await env["store"].get(TENANT, REPO, 7)
    row.status = "failed"
    row.queued_sha = sha
    row.attempts = attempts
    row.failed_at = NOW - timedelta(minutes=minutes_ago)
    await env["store"].save(row)


async def test_a_failed_review_is_retried_after_the_cooldown(env):
    env["github"].add(make_pr(7, SHA1))
    cfg = _cfg(watch_repos=True, chat_space="")
    await _run(env, cfg)
    await _fail_review(env, minutes_ago=10)
    assert (await _run(env, cfg))["tasks_created"] == 0  # cooling down
    await _fail_review(env, minutes_ago=61)
    env["tasks"].by_key.clear()  # the first task was resolved
    assert (await _run(env, cfg))["tasks_created"] == 1


async def test_a_failed_review_stops_after_three_attempts_on_one_head(env):
    env["github"].add(make_pr(7, SHA1))
    cfg = _cfg(watch_repos=True, chat_space="")
    await _run(env, cfg)
    await _fail_review(env, attempts=3, minutes_ago=600)
    env["tasks"].by_key.clear()
    assert (await _run(env, cfg))["tasks_created"] == 0
    env["github"].add(make_pr(7, SHA2))  # a new head starts over
    assert (await _run(env, cfg))["tasks_created"] == 1


# ── real-thread shapes (synthetic) ──────────────────────────────────────


async def _thread_of_two(env):
    env["github"].add(make_pr(7, SHA1))
    env["github"].add(make_pr(8, SHA1))
    env["chat"].post(f"two for review {URL} https://github.com/{REPO}/pull/8")
    await _run(env)
    await _finish(env, 7)
    await _finish(env, 8)
    env["github"].add(make_pr(7, SHA2))
    env["github"].add(make_pr(8, SHA2))


async def test_a_reply_naming_one_pr_of_a_multi_pr_thread_rereviews_only_that_one(env):
    await _thread_of_two(env)
    env["chat"].post("8 is ready for re-review", reply=True, time="2026-10-05T10:05:00.000000Z")
    await _run(env)
    assert [t["spec"].row.number for t in env["tasks"].created[2:]] == [8]


async def test_a_reply_naming_no_pr_rereviews_the_whole_thread(env):
    await _thread_of_two(env)
    env["chat"].post("updated, ready for re-review", reply=True, time="2026-10-05T10:05:00.000000Z")
    await _run(env)
    assert sorted(t["spec"].row.number for t in env["tasks"].created[2:]) == [7, 8]


@pytest.mark.parametrize(
    "text",
    ["#7: Approved", "#7: Comments/change request", "#7: No changes?", "Changes requested on both"],
)
async def test_review_status_lines_in_the_thread_trigger_nothing(env, text):
    env["github"].add(make_pr(7, SHA1))
    env["chat"].post(f"review {URL}")
    await _run(env)
    await _finish(env)
    env["github"].add(make_pr(7, SHA2))
    env["chat"].post(text, reply=True, time="2026-10-05T10:05:00.000000Z")
    summary = await _run(env)
    assert summary["tasks_created"] == 0 and summary["triggers"] == 0

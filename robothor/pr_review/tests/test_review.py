"""prepare → (Claude Code) → finalize, with every outside system faked."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from robothor.pr_review.config import ReviewerConfig
from robothor.pr_review.review import finalize, prepare
from robothor.pr_review.schema import REVIEW_OUTPUT_SCHEMA
from robothor.pr_review.store import MemoryStore, PrReviewRow
from robothor.pr_review.tests.fakes import REPO, FakeChat, FakeGitHub, make_pr

TENANT = "test-tenant"
SHA1 = "1" * 40
SHA2 = "2" * 40


@dataclass
class FakeJob:
    id: str = "job-1"
    status: str = "done"
    base_sha: str = SHA1
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)


class FakePoster:
    def __init__(self) -> None:
        self.reviews: list[dict[str, Any]] = []
        self.replies: list[tuple[int, str]] = []
        self.resolved: list[list[int]] = []
        self.next_id = 500

    async def create_review(self, args: dict[str, Any]) -> dict[str, Any]:
        self.reviews.append(args)
        self.next_id += 1
        inline = [
            i for i in args["issues"] if i["severity"] in ("blocker", "major") and i.get("line")
        ]
        return {
            "review_id": self.next_id,
            "url": f"https://github.com/{REPO}/pull/7#pullrequestreview-{self.next_id}",
            "inline_count": len(inline),
            "comments": [
                {"id": 9000 + n, "path": i["path"], "line": i["line"]} for n, i in enumerate(inline)
            ],
        }

    async def reply(self, repo: str, number: int, comment_id: int, body: str) -> dict[str, Any]:
        self.replies.append((comment_id, body))
        return {"comment_id": 1}

    async def resolve(self, repo: str, number: int, review_ids: list[int]) -> dict[str, Any]:
        self.resolved.append(review_ids)
        return {"resolved": 1}


def _issue(severity: str, line: int | None = 3, title: str = "t") -> dict[str, Any]:
    return {
        "path": "src/a.py",
        "line": line,
        "start_line": None,
        "side": "RIGHT",
        "severity": severity,
        "title": title,
        "body": "verified: unit test",
    }


def _output(verdict: str, issues: list, prior: list | None = None) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "summary": "Looks at X.",
        "issues": issues,
        "prior_issues": prior or [],
    }


@pytest.fixture
async def env():
    return await make_env()


async def make_env() -> dict[str, Any]:
    """A pull request under review: prepare has bound job-1 to head SHA1."""
    store = MemoryStore()
    github = FakeGitHub()
    github.add(make_pr(7, SHA1))
    row = PrReviewRow(
        tenant_id=TENANT,
        repo=REPO,
        number=7,
        url=f"https://github.com/{REPO}/pull/7",
        status="reviewing",
        queued_sha=SHA1,
        job_id="job-1",
        chat_space="spaces/AAAA",
        chat_thread="spaces/AAAA/threads/t1",
        chat_message="spaces/AAAA/messages/m1",
        chat_poster="users/alice",
    )
    await store.save(row)
    return {"store": store, "github": github, "chat": FakeChat(), "poster": FakePoster()}


async def _finalize(env, job, cfg=None, **kw):
    return await finalize(
        cfg or ReviewerConfig(repos=(REPO,)),
        env["store"],
        TENANT,
        REPO,
        7,
        job=job,
        github=env["github"],
        poster=env["poster"],
        chat=env["chat"],
        **kw,
    )


async def test_model_approve_with_a_blocker_posts_request_changes(env):
    job = FakeJob(result={"structured_output": _output("APPROVE", [_issue("blocker")])})
    result = await _finalize(env, job)
    assert env["poster"].reviews[0]["verdict"] == "REQUEST_CHANGES"
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["model_verdict"] == "APPROVE"
    assert result["verdict_overridden"] is True
    row = await env["store"].get(TENANT, REPO, 7)
    assert row.status == "changes_requested"
    assert row.last_reviewed_sha == SHA1
    assert row.review_ids == [501]
    assert row.last_review["issues"][0]["comment_id"] == 9000
    assert env["chat"].replies[-1][2] == (
        f"<https://github.com/{REPO}/pull/7|#7>: Comments/change request — 1 blocking"
    )


async def test_the_review_footer_names_the_model_effort_and_guidelines(env):
    job = FakeJob(result={"structured_output": _output("APPROVE", []), "model": "claude-opus-5-5"})
    job.effort = "high"  # type: ignore[attr-defined]
    job.task = '<review_guidelines path="g.md" sha256="42a69406da62">\n…'  # type: ignore[attr-defined]
    await _finalize(env, job)
    assert env["poster"].reviews[0]["footer_meta"] == {
        "model": "claude-opus-5-5",
        "effort": "high",
        "guidelines": "42a69406da62",
    }


async def test_clean_approval_resolves_our_threads_and_reacts(env):
    row = await env["store"].get(TENANT, REPO, 7)
    row.review_ids = [400]
    row.last_reviewed_sha = "0" * 40
    row.last_review = {"issues": [{**_issue("blocker"), "comment_id": 77}]}
    await env["store"].save(row)
    prior = [
        {"comment_id": 77, "description": "null deref", "status": "resolved", "note": "guarded"}
    ]
    job = FakeJob(result={"structured_output": _output("APPROVE", [_issue("nit")], prior)})
    result = await _finalize(env, job)
    assert result["verdict"] == "APPROVE"
    assert env["poster"].resolved == [[400, 501]]
    assert env["poster"].replies == [(77, "**Resolved** — guarded")]
    assert ("spaces/AAAA/messages/m1", "\U0001f44d") in env["chat"].reactions
    assert env["chat"].replies[-1][2].endswith(": Approved")


async def test_unresolved_prior_blocker_blocks_approval(env):
    row = await env["store"].get(TENANT, REPO, 7)
    row.review_ids = [400]
    row.last_review = {"issues": [{**_issue("major"), "comment_id": 77}]}
    await env["store"].save(row)
    prior = [{"comment_id": 77, "description": "x", "status": "unresolved", "note": "still there"}]
    job = FakeJob(result={"structured_output": _output("APPROVE", [], prior)})
    result = await _finalize(env, job)
    assert result["verdict"] == "REQUEST_CHANGES"
    assert env["poster"].resolved == []
    assert env["poster"].replies == [(77, "**Still open** — still there")]


async def test_replies_only_on_our_own_previous_comments(env):
    prior = [{"comment_id": 12345, "description": "x", "status": "resolved", "note": ""}]
    job = FakeJob(result={"structured_output": _output("COMMENT", [], prior)})
    await _finalize(env, job)
    assert env["poster"].replies == []


async def test_invalid_output_fails_without_posting(env):
    job = FakeJob(result={"structured_output": {"verdict": "APPROVE"}})
    result = await _finalize(env, job)
    assert result["status"] == "failed"
    assert env["poster"].reviews == []
    row = await env["store"].get(TENANT, REPO, 7)
    assert row.status == "failed" and "invalid review output" in row.error
    assert "re-review" in env["chat"].replies[-1][2]


async def test_failed_job_is_recorded(env):
    result = await _finalize(env, FakeJob(status="failed", error="budget exhausted"))
    assert result["status"] == "failed"
    assert env["poster"].reviews == []


async def test_running_job_is_not_finalized(env):
    result = await _finalize(env, FakeJob(status="running"))
    assert "still running" in result["error"]


async def test_head_moved_during_review_requeues(env):
    env["github"].add(make_pr(7, SHA2))
    job = FakeJob(result={"structured_output": _output("APPROVE", [])})
    result = await _finalize(env, job)
    assert result["status"] == "stale"
    assert env["poster"].reviews == []
    row = await env["store"].get(TENANT, REPO, 7)
    assert row.pending_trigger == "new_head"


async def test_ticket_rule_blocks_an_approval(env):
    job = FakeJob(result={"structured_output": _output("APPROVE", [])})
    cfg = ReviewerConfig(repos=(REPO,), require_ticket=True, ticket_prefixes=("ABC",))
    result = await _finalize(env, job, cfg)
    assert result["verdict"] == "REQUEST_CHANGES"
    assert any("[no-ticket]" in i["title"] for i in env["poster"].reviews[0]["issues"])


async def test_ticket_in_a_commit_trailer_satisfies_the_rule(env):
    env["github"].commits[(REPO, 7)] = [{"commit": {"message": "fix: x\n\nRefs: ABC-12"}}]
    job = FakeJob(result={"structured_output": _output("APPROVE", [])})
    cfg = ReviewerConfig(repos=(REPO,), require_ticket=True, ticket_prefixes=("ABC",))
    assert (await _finalize(env, job, cfg))["verdict"] == "APPROVE"


async def test_blocking_event_comment(env):
    job = FakeJob(result={"structured_output": _output("APPROVE", [_issue("major")])})
    cfg = ReviewerConfig(repos=(REPO,), blocking_event="COMMENT")
    result = await _finalize(env, job, cfg)
    assert result["verdict"] == "COMMENT"


async def test_digest_only_when_enabled(env):
    job = FakeJob(result={"structured_output": _output("APPROVE", [])})
    result = await _finalize(env, job, ReviewerConfig(repos=(REPO,), telegram_digest=True))
    assert result["digest"].startswith(f"PR review {REPO}#7: Approved")


async def test_dismiss_returns_the_row_to_its_last_state(env):
    row = await env["store"].get(TENANT, REPO, 7)
    row.status, row.job_id = "queued", ""  # an ambiguous trigger, before any prepare
    row.last_reviewed_sha = SHA1
    row.last_review = {"verdict": "REQUEST_CHANGES"}
    await env["store"].save(row)
    result = await _finalize(env, None, dismiss=True)
    assert result["status"] == "dismissed"
    assert (await env["store"].get(TENANT, REPO, 7)).status == "changes_requested"


class StartRecorder:
    def __init__(self, job_id: str = "job-9") -> None:
        self.calls: list[dict[str, Any]] = []
        self.job_id = job_id

    async def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(args)
        return {"job_id": self.job_id, "status": "queued"}


async def _queue(env) -> None:
    row = await env["store"].get(TENANT, REPO, 7)
    row.status, row.job_id = "queued", ""
    await env["store"].save(row)


async def test_prepare_builds_the_claude_code_arguments(env, tmp_path):
    calls: dict[str, Any] = {}
    start = StartRecorder()
    await _queue(env)

    async def fake_checkout(dest, **kw):
        calls.update(kw, dest=dest)
        return tmp_path

    async def fake_read(path, sha, rel):
        return "Use snake_case." if rel == "CLAUDE.md" and sha == "origin/main" else None

    cfg = ReviewerConfig(repos=(REPO,), clone_root=str(tmp_path / "clones"), review_model="sonnet")
    result = await prepare(
        cfg,
        env["store"],
        TENANT,
        REPO,
        7,
        github=env["github"],
        skill_text="# Review guidelines\nWalk every lens.",
        token="t",
        checkout=fake_checkout,
        reader=fake_read,
        start_job=start,
    )
    [args] = start.calls
    assert result["job_id"] == "job-9" and "start_args" not in result
    assert args["mode"] == "review" and args["base_ref"] == SHA1
    assert args["repo_path"] == str(tmp_path)
    assert args["json_schema"] == REVIEW_OUTPUT_SCHEMA
    assert args["grant_github"] is True and args["model"] == "sonnet"
    assert "Walk every lens." in args["task"]
    assert '<repository_rules path="CLAUDE.md">' in args["task"]
    assert "initial review" in args["task"]
    assert calls["dest"] == Path(tmp_path / "clones" / "acme" / "widgets")
    assert calls["head_sha"] == SHA1 and calls["number"] == 7
    row = await env["store"].get(TENANT, REPO, 7)
    assert (row.status, row.job_id, row.queued_sha) == ("reviewing", "job-9", SHA1)


async def test_prepare_incremental_lists_previous_findings(env, tmp_path):
    await _queue(env)
    row = await env["store"].get(TENANT, REPO, 7)
    row.last_reviewed_sha = "0" * 40
    row.last_review = {
        "verdict": "REQUEST_CHANGES",
        "issues": [{**_issue("major"), "comment_id": 77}],
    }
    await env["store"].save(row)

    async def fake_checkout(dest, **kw):
        return tmp_path

    async def fake_read(*a):
        return None

    result = await prepare(
        ReviewerConfig(repos=(REPO,)),
        env["store"],
        TENANT,
        REPO,
        7,
        github=env["github"],
        skill_text="guide",
        token="",
        checkout=fake_checkout,
        reader=fake_read,
        start_job=(start := StartRecorder()),
    )
    assert result["mode"] == "incremental"
    task = start.calls[0]["task"]
    assert "RE-REVIEW" in task and '"comment_id": 77' in task
    assert f"git diff {'0' * 40}..HEAD" in task


async def test_prepare_skips_an_already_reviewed_head(env):
    await _queue(env)
    row = await env["store"].get(TENANT, REPO, 7)
    row.last_reviewed_sha = SHA1
    await env["store"].save(row)
    result = await prepare(
        ReviewerConfig(repos=(REPO,)),
        env["store"],
        TENANT,
        REPO,
        7,
        github=env["github"],
        skill_text="g",
        token="",
        start_job=StartRecorder(),
    )
    assert result["skip"] is True


# ── a posting failure re-posts the written review, never a new job ──────


class FlakyPoster(FakePoster):
    """GitHub refuses the first ``fails`` posts with a transient 5xx."""

    def __init__(self, fails: int = 1) -> None:
        super().__init__()
        self.fails = fails

    async def create_review(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.fails:
            self.fails -= 1
            self.reviews.append(args)
            return {"error": "GitHub API error 500: no detail", "transient": True}
        return await super().create_review(args)


def _jobs(*jobs: FakeJob):
    by_id = {j.id: j for j in jobs}

    async def lookup(job_id: str):
        job = by_id.get(job_id)
        return (job.status, None) if job else None

    return lookup


async def _prepare_again(env, *, job_status, start=None):
    start = start or StartRecorder()

    async def no_checkout(*a, **kw):
        raise AssertionError("a resumed post must not check the pull request out again")

    result = await prepare(
        ReviewerConfig(repos=(REPO,)),
        env["store"],
        TENANT,
        REPO,
        7,
        github=env["github"],
        skill_text="g",
        token="",
        checkout=no_checkout,
        start_job=start,
        job_status=job_status,
    )
    return result, start


async def test_a_posting_failure_resumes_without_a_new_job(env):
    env["poster"] = FlakyPoster(fails=1)
    job = FakeJob(result={"structured_output": _output("APPROVE", [_issue("nit")])})
    failed = await _finalize(env, job)
    assert failed["status"] == "failed"
    assert (await env["store"].get(TENANT, REPO, 7)).status == "failed"

    result, start = await _prepare_again(env, job_status=_jobs(job))
    assert result["resumed"] is True and result["job_id"] == "job-1"
    assert start.calls == []
    assert (await env["store"].get(TENANT, REPO, 7)).status == "posting"

    posted = await _finalize(env, job)
    assert posted["status"] == "posted" and posted["verdict"] == "APPROVE"
    assert len(env["poster"].reviews) == 2  # the refused post, then the one that landed
    row = await env["store"].get(TENANT, REPO, 7)
    assert row.status == "approved" and row.last_reviewed_sha == SHA1


async def test_a_resume_after_the_review_landed_does_not_post_it_again(env):
    job = FakeJob(result={"structured_output": _output("COMMENT", [_issue("minor")])})
    row = await env["store"].get(TENANT, REPO, 7)
    # Posting crashed after the review landed; the intake expired the row to failed.
    row.status = "failed"
    row.last_review = {
        "pending": {
            "job_id": "job-1",
            "prior_issues_seen": [],
            "prior_review_ids": [],
            "ticket": None,
            "done": {"review": {"review_id": 777, "url": "u", "inline_count": 0, "issues": []}},
        }
    }
    await env["store"].save(row)

    result, start = await _prepare_again(env, job_status=_jobs(job))
    assert result["resumed"] is True and start.calls == []
    posted = await _finalize(env, job)
    assert posted["status"] == "posted"
    assert env["poster"].reviews == []
    assert (await env["store"].get(TENANT, REPO, 7)).review_ids == [777]


async def _failed_while_posting(env) -> FakeJob:
    env["poster"] = FlakyPoster(fails=1)
    job = FakeJob(result={"structured_output": _output("APPROVE", [])})
    await _finalize(env, job)
    return job


async def test_a_moved_head_reviews_afresh(env, tmp_path):
    job = await _failed_while_posting(env)
    env["github"].add(make_pr(7, SHA2))

    async def fake_checkout(dest, **kw):
        return tmp_path

    async def fake_read(path, sha, rel):
        return None

    start = StartRecorder()
    result = await prepare(
        ReviewerConfig(repos=(REPO,)),
        env["store"],
        TENANT,
        REPO,
        7,
        github=env["github"],
        skill_text="g",
        token="",
        checkout=fake_checkout,
        reader=fake_read,
        start_job=start,
        job_status=_jobs(job),
    )
    assert "resumed" not in result and len(start.calls) == 1


async def test_a_vanished_job_reviews_afresh(env, tmp_path):
    await _failed_while_posting(env)

    async def fake_checkout(dest, **kw):
        return tmp_path

    async def fake_read(path, sha, rel):
        return None

    start = StartRecorder()
    result = await prepare(
        ReviewerConfig(repos=(REPO,)),
        env["store"],
        TENANT,
        REPO,
        7,
        github=env["github"],
        skill_text="g",
        token="",
        checkout=fake_checkout,
        reader=fake_read,
        start_job=start,
        job_status=_jobs(),
    )
    assert "resumed" not in result and len(start.calls) == 1


async def test_a_failure_before_posting_reviews_afresh(env, tmp_path):
    job = FakeJob(status="failed", error="crashed")
    await _finalize(env, job)
    assert (await env["store"].get(TENANT, REPO, 7)).status == "failed"

    async def fake_checkout(dest, **kw):
        return tmp_path

    async def fake_read(path, sha, rel):
        return None

    start = StartRecorder()
    result = await prepare(
        ReviewerConfig(repos=(REPO,)),
        env["store"],
        TENANT,
        REPO,
        7,
        github=env["github"],
        skill_text="g",
        token="",
        checkout=fake_checkout,
        reader=fake_read,
        start_job=start,
        job_status=_jobs(job),
    )
    assert "resumed" not in result and len(start.calls) == 1


async def test_the_intakes_retry_resumes_the_post(env):
    """The real hand-off: the intake re-queues the failed row (status queued)
    and files a task; prepare then re-posts the written review."""
    from datetime import UTC, datetime, timedelta

    from robothor.pr_review.intake import Intake
    from robothor.pr_review.tests.fakes import FakeTasks

    env["poster"] = FlakyPoster(fails=1)
    job = FakeJob(result={"structured_output": _output("APPROVE", [])})
    await _finalize(env, job)
    cfg = ReviewerConfig(repos=(REPO,))
    tasks = FakeTasks()
    intake = Intake(
        cfg,
        env["store"],
        TENANT,
        tasks=tasks,
        github=env["github"],
        chat=env["chat"],
        now=datetime.now(UTC) + timedelta(minutes=cfg.retry_cooldown_minutes + 1),
    )
    await intake.run(poll=False)
    assert (await env["store"].get(TENANT, REPO, 7)).status == "queued"

    result, start = await _prepare_again(env, job_status=_jobs(job))
    assert result.get("resumed") is True and start.calls == []
    assert (await _finalize(env, job))["status"] == "posted"

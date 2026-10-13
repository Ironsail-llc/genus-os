"""The pr_review_* tool handlers: gating and wiring. The logic is tested in robothor/pr_review."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import pr_review
from robothor.pr_review.config import ReviewerConfig
from robothor.pr_review.store import MemoryStore
from robothor.pr_review.tests.fakes import REPO, FakeChat, FakeGitHub, FakeTasks, make_pr

_CTX = ToolContext(agent_id="pr-reviewer", tenant_id="test-tenant")
_BENCH = ToolContext(agent_id="pr-reviewer", tenant_id="test-tenant", is_benchmark=True)


@pytest.mark.parametrize(
    "name",
    [
        "pr_review_intake",
        "pr_review_prepare",
        "pr_review_finalize",
        "pr_review_work",
        "pr_review_status",
    ],
)
async def test_every_tool_refuses_a_benchmark_run(name):
    result = await pr_review.HANDLERS[name]({"repo": REPO, "number": 1}, _BENCH)
    assert result["guard"] == "is_benchmark"


def test_tools_are_opt_in_and_side_effects():
    from robothor.engine.benchmark_sandbox import EXTERNAL_SIDE_EFFECT_TOOLS
    from robothor.engine.tools.constants import OPT_IN_TOOLS, PR_REVIEW_TOOLS

    assert set(pr_review.HANDLERS) == PR_REVIEW_TOOLS
    assert PR_REVIEW_TOOLS <= OPT_IN_TOOLS
    assert PR_REVIEW_TOOLS <= EXTERNAL_SIDE_EFFECT_TOOLS


async def test_intake_unconfigured_does_nothing():
    with patch("robothor.pr_review.config.load_config", return_value=ReviewerConfig()):
        result = await pr_review._intake({}, _CTX)
    assert result["configured"] is False and result["open_tasks"] == 0


async def test_intake_wires_the_factories():
    store, github, chat, tasks = MemoryStore(), FakeGitHub(), FakeChat(), FakeTasks()
    github.add(make_pr(7, "1" * 40))
    chat.post(f"https://github.com/{REPO}/pull/7", time="2099-01-01T00:00:00.000000Z")
    cfg = ReviewerConfig(repos=(REPO,), watch_repos=False, chat_space="spaces/AAAA")
    with (
        patch("robothor.pr_review.config.load_config", return_value=cfg),
        patch.object(pr_review, "_store", return_value=store),
        patch.object(pr_review, "_github", return_value=github),
        patch.object(pr_review, "_chat", return_value=chat),
        patch.object(pr_review, "_tasks", return_value=tasks),
    ):
        result = await pr_review._intake({}, _CTX)
    assert result["tasks_created"] == 1 and result["open_tasks"] == 1


async def test_finalize_requires_a_job_or_dismiss():
    result = await pr_review._finalize({"repo": REPO, "number": 7}, _CTX)
    assert "job_id is required" in result["error"]


async def test_finalize_refuses_a_code_mode_job():
    manager = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(mode="code")))
    with patch("robothor.engine.coding.jobs.get_manager", return_value=manager):
        result = await pr_review._finalize(
            {"repo": REPO, "number": 7, "job_id": "00000000-0000-4000-8000-000000000001"}, _CTX
        )
    assert "mode=review" in result["error"]


async def test_prepare_validates_arguments():
    result = await pr_review._prepare({"repo": "not a repo", "number": 7}, _CTX)
    assert "owner/repo" in result["error"]


async def test_prepare_needs_the_skill():
    with patch.object(pr_review, "_skill_text", return_value=None):
        result = await pr_review._prepare({"repo": REPO, "number": 7}, _CTX)
    assert "skill" in result["error"]


async def test_prepare_starts_the_job_itself_under_the_callers_context():
    seen: dict = {}

    async def fake_start(args, ctx):
        seen.update(args=args, ctx=ctx)
        return {"job_id": "job-1", "status": "queued"}

    async def fake_prepare(*a, start_job, **kw):
        return await start_job({"task": "review", "mode": "review"})

    with (
        patch.object(pr_review, "_skill_text", return_value="guide"),
        patch.object(pr_review, "_github", return_value=FakeGitHub()),
        patch.object(pr_review, "_store", return_value=MemoryStore()),
        patch("robothor.pr_review.config.load_config", return_value=ReviewerConfig()),
        patch("robothor.engine.tools.handlers.github_api._get_token", return_value=""),
        patch("robothor.engine.tools.handlers.claude_code._start", fake_start),
        patch("robothor.pr_review.review.prepare", fake_prepare),
    ):
        result = await pr_review._prepare({"repo": REPO, "number": 7}, _CTX)
    assert result["job_id"] == "job-1"
    assert seen["ctx"] is _CTX and seen["args"]["mode"] == "review"


async def test_intake_count_only_changes_nothing():
    store, github, chat, tasks = MemoryStore(), FakeGitHub(), FakeChat(), FakeTasks()
    github.add(make_pr(7, "1" * 40))
    chat.post(f"https://github.com/{REPO}/pull/7", time="2099-01-01T00:00:00.000000Z")
    cfg = ReviewerConfig(repos=(REPO,), watch_repos=True, chat_space="spaces/AAAA")
    with (
        patch("robothor.pr_review.config.load_config", return_value=cfg),
        patch.object(pr_review, "_store", return_value=store),
        patch.object(pr_review, "_github", return_value=github),
        patch.object(pr_review, "_chat", return_value=chat),
        patch.object(pr_review, "_tasks", return_value=tasks),
    ):
        result = await pr_review._intake({"count_only": True}, _CTX)
    assert result["queued_tasks"] == 0 and tasks.created == [] and chat.reactions == []


async def test_ticket_searcher_builds_a_safe_jql_and_slims_the_result():
    from robothor.engine.tools.handlers import jira

    seen: dict = {}

    async def fake_search(args, ctx):
        seen.update(args)
        return {"issues": [{"key": "ABC-9", "summary": "retry job", "status": "Backlog"}]}

    with (
        patch.object(jira, "_get_base_url", return_value="https://jira.example.com"),
        patch.object(jira, "_get_auth_header", return_value="Basic x"),
        patch.object(jira, "_jira_search", fake_search),
    ):
        found = await pr_review._ticket_searcher(_CTX)(
            "ABC", 'fix(refunds): refund "once" and never zero OR project = X'
        )
    assert found == [{"key": "ABC-9", "summary": "retry job", "status": "Backlog"}]
    jql = seen["jql"]
    assert jql.startswith('project = "ABC" AND text ~ "')
    assert jql.count('"') == 4  # the title's own quotes never reach the JQL
    assert "refunds" in jql and seen["max_results"] == 5


async def test_ticket_searcher_is_quiet_without_jira():
    from robothor.engine.tools.handlers import jira

    with patch.object(jira, "_get_base_url", return_value=""):
        assert await pr_review._ticket_searcher(_CTX)("ABC", "anything") == []


async def test_job_state_reads_the_bound_job_for_the_tenant():
    """The intake's orphaned-review check: status and an aware finish time."""
    from datetime import UTC, datetime

    jobs = {
        "done": SimpleNamespace(status="done", finished_at="2026-10-05T10:00:00+00:00"),
        "naive": SimpleNamespace(status="failed", finished_at="2026-10-05T10:00:00"),
        "running": SimpleNamespace(status="running", finished_at=None),
    }
    seen = []

    async def get(job_id, tenant_id):
        seen.append(tenant_id)
        return jobs.get(job_id)

    with patch("robothor.engine.coding.jobs.get_manager", return_value=SimpleNamespace(get=get)):
        lookup = pr_review._job_state("test-tenant")
        finished = datetime(2026, 10, 5, 10, tzinfo=UTC)
        assert await lookup("done") == ("done", finished)
        assert await lookup("naive") == ("failed", finished)
        assert await lookup("running") == ("running", None)
        assert await lookup("missing") is None
    assert set(seen) == {"test-tenant"}


# ── pr_review_work: the deterministic worker ─────────────────────────────

_WORKFLOW = ToolContext(
    agent_id="workflow:pr-review-run", tenant_id="test-tenant", user_role="service"
)


def _fake_manager(seen: dict):
    async def start(**kw):
        seen.setdefault("starts", []).append(kw)
        return SimpleNamespace(
            id="job-1",
            status="queued",
            branch=None,
            worktree_path="",
            max_rounds=2,
            max_budget_usd=None,
        )

    async def wait(job_id, tenant_id, timeout_s, agent_id=None):
        seen.setdefault("waits", []).append((job_id, tenant_id))
        return SimpleNamespace(status="done")

    async def get(job_id, tenant_id, agent_id=None):
        return SimpleNamespace(id=job_id, mode="review", status="done")

    return SimpleNamespace(start=start, wait=wait, get=get)


async def test_work_runs_review_jobs_as_the_reviewer_agent_not_the_workflow(tmp_path):
    """grant_github is honored only for an agent whose own manifest names
    GITHUB_TOKEN: the job must start as pr-reviewer, never as the workflow."""
    from robothor.pr_review.tests.fakes import FakeQueue, review_task

    seen: dict = {}
    queue = FakeQueue([review_task("t1", 7)])
    cfg = ReviewerConfig(repos=(REPO,), workers=3, status_file=str(tmp_path / "status.md"))

    async def fake_prepare(*a, start_job, **kw):
        started = await start_job(
            {
                "task": "review",
                "repo_path": str(tmp_path),
                "acceptance": {},
                "mode": "review",
                "grant_github": True,
            }
        )
        return {"job_id": started["job_id"], "head_sha": "a" * 40}

    async def fake_finalize(cfg, store, tenant_id, repo, number, *, job, **kw):
        seen["finalized"] = (tenant_id, repo, number, job.id)
        return {"status": "posted", "verdict": "APPROVE", "review_url": "https://x/r", "digest": ""}

    with (
        patch("robothor.pr_review.config.load_config", return_value=cfg),
        patch.object(pr_review, "_task_queue", return_value=queue),
        patch.object(pr_review, "_skill_text", return_value="guide"),
        patch.object(pr_review, "_github", return_value=FakeGitHub()),
        patch.object(pr_review, "_store", return_value=MemoryStore()),
        patch("robothor.engine.tools.handlers.github_api._get_token", return_value=""),
        patch("robothor.engine.coding.jobs.get_manager", return_value=_fake_manager(seen)),
        patch("robothor.pr_review.review.prepare", fake_prepare),
        patch("robothor.pr_review.review.finalize", fake_finalize),
    ):
        result = await pr_review._work({}, _WORKFLOW)
    assert seen["starts"][0]["agent_id"] == "pr-reviewer"
    assert seen["starts"][0]["grant_github"] is True
    assert seen["starts"][0]["tenant_id"] == "test-tenant"
    assert seen["waits"] == [("job-1", "test-tenant")]
    assert seen["finalized"] == ("test-tenant", REPO, 7, "job-1")
    assert queue.resolved == {"t1": "APPROVE: https://x/r"}
    assert result["processed"] == 1


def test_the_github_token_is_resolved_only_for_the_reviewer_agent():
    from robothor.engine.coding import jobs

    grants = {"pr-reviewer": ("GITHUB_TOKEN",)}
    with (
        patch("robothor.engine.exec_env.grants_for_agent", side_effect=lambda a: grants.get(a, ())),
        patch(
            "robothor.secrets.resolve_secret",
            return_value=SimpleNamespace(value="ghs_test"),
        ),
    ):
        assert jobs._default_github_token("pr-reviewer", "test-tenant") == "ghs_test"
        assert jobs._default_github_token("workflow:pr-review-run", "test-tenant") is None


async def test_work_runs_once_per_tenant_at_a_time():
    import asyncio

    from robothor.pr_review.tests.fakes import FakeQueue, review_task

    gate = asyncio.Event()

    class SlowQueue(FakeQueue):
        async def list_open(self):
            await gate.wait()
            return []

    cfg = ReviewerConfig(repos=(REPO,), workers=3)
    with (
        patch("robothor.pr_review.config.load_config", return_value=cfg),
        patch.object(pr_review, "_task_queue", return_value=SlowQueue([review_task("t", 1)])),
    ):
        first = asyncio.create_task(pr_review._work({}, _WORKFLOW))
        await asyncio.sleep(0.01)
        second = await pr_review._work({}, _WORKFLOW)
        gate.set()
        await first
    assert second == {"skipped": "running", "processed": 0}


async def test_work_unconfigured_does_nothing():
    with patch("robothor.pr_review.config.load_config", return_value=ReviewerConfig()):
        result = await pr_review._work({}, _WORKFLOW)
    assert result["configured"] is False and result["processed"] == 0


async def test_the_digest_goes_to_the_operators_telegram():
    sent = []

    async def sender(chat_id, text):
        sent.append((chat_id, text))
        return [object()]

    with (
        patch("robothor.engine.delivery.get_telegram_sender", return_value=sender),
        patch(
            "robothor.engine.config.EngineConfig.from_env",
            return_value=SimpleNamespace(default_chat_id="12345"),
        ),
    ):
        notify = pr_review._telegram_notify()
        assert notify is not None
        await notify("PR review acme/widgets#7: Approved")
    assert sent == [("12345", "PR review acme/widgets#7: Approved")]


def test_no_telegram_no_notify():
    with patch("robothor.engine.delivery.get_telegram_sender", return_value=None):
        assert pr_review._telegram_notify() is None


def test_the_worker_gets_its_own_deadline_from_the_timeout_registry():
    from robothor.engine.tool_timeouts import resolve_tool_timeout
    from robothor.pr_review.worker import DEADLINE_SECONDS

    budget = resolve_tool_timeout("pr_review_work", 120)
    assert DEADLINE_SECONDS < budget <= 3300
    assert resolve_tool_timeout("pr_review_prepare", 120) == 600


# ── pr_review_status: the report ────────────────────────────────────────


async def test_status_wires_the_store_tasks_and_jobs():
    from datetime import UTC, datetime, timedelta

    from robothor.pr_review.store import PrReviewRow

    store = MemoryStore()
    await store.save(
        PrReviewRow(tenant_id="test-tenant", repo=REPO, number=7, status="reviewing", job_id="j1")
    )
    resolved = datetime.now(UTC) - timedelta(minutes=5)
    tasks = [
        {
            "id": "t1",
            "body": f"repo: {REPO}\nnumber: 8\n\nprReview: {REPO}#8@{'a' * 40}\n",
            "status": "DONE",
            "createdAt": (resolved - timedelta(minutes=10)).isoformat(),
            "resolvedAt": resolved.isoformat(),
            "resolution": "APPROVE: https://x/r",
        }
    ]
    manager = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(status="running", created_at=resolved.isoformat())
        )
    )
    cfg = ReviewerConfig(repos=(REPO,), workers=3)
    with (
        patch("robothor.pr_review.config.load_config", return_value=cfg),
        patch.object(pr_review, "_store", return_value=store),
        patch.object(pr_review, "_review_tasks", return_value=AsyncMock(return_value=tasks)),
        patch("robothor.engine.coding.jobs.get_manager", return_value=manager),
    ):
        report = await pr_review._status({"since_hours": 48}, _CTX)
    assert report["now"]["in_flight"][0]["job_status"] == "running"
    assert report["now"]["free_slots"] == 2
    assert report["window"]["posted"] == {"APPROVE": 1}
    assert report["window"]["latency_minutes"]["median"] == 10.0


async def test_status_rejects_a_bad_window():
    with patch("robothor.pr_review.config.load_config", return_value=ReviewerConfig(repos=(REPO,))):
        result = await pr_review._status({"since_hours": "lots"}, _CTX)
    assert "since_hours" in result["error"]

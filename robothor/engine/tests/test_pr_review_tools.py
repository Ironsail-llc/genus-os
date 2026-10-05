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


@pytest.mark.parametrize("name", ["pr_review_intake", "pr_review_prepare", "pr_review_finalize"])
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

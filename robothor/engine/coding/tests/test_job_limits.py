"""Per-job effort, turn cap and round timeout: a review job sets its own limits."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from robothor.engine.coding import jobs as jobs_mod
from robothor.engine.coding.jobs import Acceptance, CodingJobManager, JobStatus, MemoryJobStore
from robothor.engine.coding.tests.test_jobs import TENANT, FakeRunner, _manager, _result, _start
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import claude_code

if TYPE_CHECKING:
    from robothor.engine.coding.runner import ClaudeInvocation


async def test_per_job_effort_turns_and_round_timeout_reach_the_runner(git_repo, coding_env):
    seen: list[tuple[ClaudeInvocation, float]] = []

    async def runner(inv, *, env, timeout_s, on_event=None):
        seen.append((inv, timeout_s))
        return _result(structured_output={"verdict": "ok"})

    mgr = _manager(runner)
    job = await _start(
        mgr,
        git_repo,
        mode="review",
        acceptance=Acceptance(verify_command=None, require_commit=False),
        effort="xhigh",
        max_turns=40,
        round_timeout_s=1800,
    )
    job = await mgr.wait(job.id, TENANT, timeout_s=20)
    assert job.status == JobStatus.DONE
    [(inv, timeout)] = seen
    assert inv.effort == "xhigh"
    assert inv.max_turns == 40
    assert timeout == 1800
    assert (job.effort, job.max_turns, job.round_timeout_s) == ("xhigh", 40, 1800)


async def test_jobs_without_per_job_limits_use_the_instance_settings(git_repo, coding_env):
    seen: list[tuple[ClaudeInvocation, float]] = []

    async def runner(inv, *, env, timeout_s, on_event=None):
        seen.append((inv, timeout_s))
        return _result()

    mgr = _manager(runner)
    job = await _start(mgr, git_repo, mode="review", acceptance=Acceptance(verify_command=None))
    await mgr.wait(job.id, TENANT, timeout_s=20)
    inv, timeout = seen[0]
    assert inv.effort is None
    assert inv.max_turns == 80
    assert timeout == 3600


async def test_start_refuses_an_unknown_effort(git_repo, coding_env):
    mgr = _manager(FakeRunner())
    with pytest.raises(ValueError, match="effort"):
        await _start(
            mgr,
            git_repo,
            mode="review",
            acceptance=Acceptance(verify_command=None),
            effort="turbo",
        )


@pytest.fixture
def manager(coding_env):
    mgr = CodingJobManager(
        store=MemoryJobStore(), runner=FakeRunner(), token_resolver=lambda t: "tok"
    )
    jobs_mod.use_manager(mgr)
    yield mgr
    jobs_mod.use_manager(None)


def _ctx() -> ToolContext:
    return ToolContext(agent_id="main", run_id="", tenant_id=TENANT)


async def test_the_start_tool_passes_effort_turns_and_round_timeout(git_repo, manager):
    start = claude_code.HANDLERS["claude_code_start"]
    out = await start(
        {
            "task": "review it",
            "repo_path": str(git_repo),
            "acceptance": {},
            "mode": "review",
            "effort": "high",
            "max_turns": 80,
            "round_timeout_s": 1800,
        },
        _ctx(),
    )
    assert "error" not in out, out
    job = await manager.get(out["job_id"], TENANT)
    assert (job.effort, job.max_turns, job.round_timeout_s) == ("high", 80, 1800.0)
    await manager.wait(out["job_id"], TENANT, timeout_s=20)


async def test_the_start_tool_rejects_a_bad_effort_or_limit(git_repo, manager):
    start = claude_code.HANDLERS["claude_code_start"]
    base = {"task": "t", "repo_path": str(git_repo), "acceptance": {}, "mode": "review"}
    assert "effort" in (await start({**base, "effort": "ludicrous"}, _ctx()))["error"]
    assert "max_turns" in (await start({**base, "max_turns": "lots"}, _ctx()))["error"]
    assert "round_timeout_s" in (await start({**base, "round_timeout_s": -5}, _ctx()))["error"]


def test_the_start_schema_offers_effort_and_limits():
    from robothor.engine.tools.schemas import get_engine_schemas

    props = get_engine_schemas()["claude_code_start"]["function"]["parameters"]["properties"]
    assert props["effort"]["enum"] == ["low", "medium", "high", "xhigh", "max"]
    assert "max_turns" in props and "round_timeout_s" in props

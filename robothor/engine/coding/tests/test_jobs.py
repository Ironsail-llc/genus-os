"""The completion loop: Claude Code's word is never the verdict, the verify is.

A fake runner stands in for ``claude -p``. Each scripted round does something
to the worktree (or nothing) and reports a result, so the loop is exercised
against a real git repository and a real verify subprocess.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from robothor.engine.coding.jobs import (
    Acceptance,
    CodingJobManager,
    JobStatus,
    MemoryJobStore,
)
from robothor.engine.coding.runner import ClaudeInvocation, ClaudeResult, ProgressEvent
from robothor.engine.session_goal import GoalEvidence, validate_evidence

if TYPE_CHECKING:
    from collections.abc import Callable

SESSION = "5f0c7a52-0000-4000-8000-0000000000aa"
TENANT = "test-tenant"

#: Passes once ok.txt exists. No shell: the loop runs it via shlex + exec.
VERIFY = f"{sys.executable} -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('ok.txt').exists() else 1)\""


def _result(**kw: Any) -> ClaudeResult:
    base = {
        "session_id": SESSION,
        "is_error": False,
        "subtype": "success",
        "result_text": "done",
        "total_cost_usd": 0.10,
        "num_turns": 4,
    }
    base.update(kw)
    return ClaudeResult(**base)


def _commit_ok(cwd: Path) -> None:
    (cwd / "ok.txt").write_text("ok")
    subprocess.run(["git", "add", "ok.txt"], cwd=cwd, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix"], cwd=cwd, check=True)


def _write_ok_no_commit(cwd: Path) -> None:
    (cwd / "ok.txt").write_text("ok")


def _nothing(cwd: Path) -> None:
    return None


class FakeRunner:
    """Scripted stand-in for ``run_claude``."""

    def __init__(
        self, *rounds: Callable[[Path], None], result: Callable[[], ClaudeResult] | None = None
    ):
        self.rounds = list(rounds)
        self.calls: list[ClaudeInvocation] = []
        self.envs: list[dict[str, str]] = []
        self._result = result or _result

    async def __call__(self, inv, *, env, timeout_s, on_event=None):
        self.calls.append(inv)
        self.envs.append(env)
        action = self.rounds.pop(0) if self.rounds else _nothing
        if on_event is not None:
            on_event(ProgressEvent(kind="tool_use", summary="Bash: pytest -q"))
        action(Path(inv.cwd))
        return self._result()


def _manager(runner, store=None, **kw) -> CodingJobManager:
    return CodingJobManager(
        store=store or MemoryJobStore(),
        runner=runner,
        token_resolver=lambda tenant: "test-token",
        **kw,
    )


async def _start(mgr, repo, **kw):
    args = {
        "tenant_id": TENANT,
        "agent_id": "main",
        "task": "make ok.txt exist",
        "repo_path": str(repo),
        "acceptance": Acceptance(verify_command=VERIFY, require_commit=True),
    }
    args.update(kw)
    return await mgr.start(**args)


async def test_fail_then_followup_then_pass(git_repo, coding_env):
    runner = FakeRunner(_nothing, _commit_ok)
    mgr = _manager(runner)

    job = await _start(mgr, git_repo)
    job = await mgr.wait(job.id, TENANT, timeout_s=20)

    assert job.status == JobStatus.DONE, job.error
    assert job.rounds == 2
    assert job.cost_usd == pytest.approx(0.20)
    assert job.turns == 8
    # Round two resumed the SAME session with the failing verify output.
    assert runner.calls[0].resume_session_id is None
    assert runner.calls[1].resume_session_id == SESSION
    assert "Acceptance failed" in runner.calls[1].prompt
    assert "exit code 1" in runner.calls[1].prompt
    # The token travels to claude; the job's worktree is the cwd.
    assert runner.envs[0]["CLAUDE_CODE_OAUTH_TOKEN"] == "test-token"
    assert Path(runner.calls[0].cwd) == coding_env / job.id

    verify = job.result["verify"]
    assert verify["passed"] is True and verify["exit_code"] == 0
    assert len(verify["output_sha256"]) == 64
    assert job.result["commit_sha"] == job.result["commits"][0]


async def test_evidence_is_in_the_shape_session_goal_accepts(git_repo, coding_env):
    mgr = _manager(FakeRunner(_commit_ok))
    job = await mgr.wait((await _start(mgr, git_repo)).id, TENANT, timeout_s=20)

    assert job.status == JobStatus.DONE
    kinds = {e["kind"] for e in job.evidence}
    assert kinds == {"test_run", "commit"}
    for raw in job.evidence:
        item = GoalEvidence(kind=raw["kind"], summary=raw["summary"], reference=raw["reference"])
        ok, why = validate_evidence(item, workspace=git_repo)
        assert ok, why


async def test_max_rounds_exhaustion_fails_the_job(git_repo, coding_env):
    runner = FakeRunner(_nothing, _nothing, _nothing, _nothing)
    mgr = _manager(runner)

    job = await mgr.wait((await _start(mgr, git_repo, max_rounds=2)).id, TENANT, timeout_s=20)

    assert job.status == JobStatus.FAILED
    assert job.rounds == 2
    assert len(runner.calls) == 2
    assert "acceptance" in job.error.lower()
    assert job.result["verify"]["passed"] is False


async def test_a_passing_verify_without_a_commit_is_not_done(git_repo, coding_env):
    runner = FakeRunner(_write_ok_no_commit, _commit_ok)
    mgr = _manager(runner)

    job = await mgr.wait((await _start(mgr, git_repo)).id, TENANT, timeout_s=20)

    assert job.status == JobStatus.DONE
    assert job.rounds == 2
    assert "commit" in runner.calls[1].prompt.lower()


async def test_budget_exhaustion_stops_the_loop(git_repo, coding_env):
    runner = FakeRunner(_nothing, _nothing, result=lambda: _result(total_cost_usd=0.6))
    mgr = _manager(runner)

    job = await mgr.wait(
        (await _start(mgr, git_repo, max_budget_usd=1.0, max_rounds=5)).id, TENANT, timeout_s=20
    )

    assert job.status == JobStatus.FAILED
    assert "budget" in job.error.lower()
    assert len(runner.calls) == 2
    # Each call is told only what is left of the job's budget.
    assert runner.calls[0].max_budget_usd == pytest.approx(1.0)
    assert runner.calls[1].max_budget_usd == pytest.approx(0.4)


async def test_review_mode_needs_no_verify_and_uses_the_readonly_preset(git_repo, coding_env):
    runner = FakeRunner(_nothing, result=lambda: _result(structured_output={"verdict": "ok"}))
    mgr = _manager(runner)

    job = await _start(
        mgr,
        git_repo,
        mode="review",
        acceptance=Acceptance(verify_command=None, require_commit=False),
        json_schema={"type": "object"},
    )
    job = await mgr.wait(job.id, TENANT, timeout_s=20)

    assert job.status == JobStatus.DONE
    assert job.result["structured_output"] == {"verdict": "ok"}
    assert "Edit" in runner.calls[0].disallowed_tools
    assert runner.calls[0].json_schema == {"type": "object"}
    assert job.branch is None  # detached: review never commits


async def test_code_mode_requires_an_acceptance_check(git_repo, coding_env):
    mgr = _manager(FakeRunner())
    with pytest.raises(ValueError, match="acceptance"):
        await _start(
            mgr, git_repo, acceptance=Acceptance(verify_command=None, require_commit=False)
        )


async def test_a_missing_token_fails_fast_with_the_fix(git_repo, coding_env):
    mgr = CodingJobManager(
        store=MemoryJobStore(), runner=FakeRunner(), token_resolver=lambda t: None
    )
    job = await mgr.wait((await _start(mgr, git_repo)).id, TENANT, timeout_s=20)

    assert job.status == JobStatus.FAILED
    assert "claude-code login" in job.error


async def test_other_tenants_cannot_see_the_job(git_repo, coding_env):
    mgr = _manager(FakeRunner(_commit_ok))
    job = await _start(mgr, git_repo)
    await mgr.wait(job.id, TENANT, timeout_s=20)

    assert await mgr.get(job.id, "other-tenant") is None
    assert await mgr.get(job.id, TENANT) is not None
    with pytest.raises(LookupError):
        await mgr.cancel(job.id, "other-tenant")


async def test_cancel_kills_the_round_and_marks_cancelled(git_repo, coding_env):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hanging_runner(inv, *, env, timeout_s, on_event=None):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return _result()

    mgr = _manager(hanging_runner)
    job = await _start(mgr, git_repo)
    await asyncio.wait_for(started.wait(), 5)

    job = await mgr.cancel(job.id, TENANT)

    assert job.status == JobStatus.CANCELLED
    assert cancelled.is_set()
    assert not (coding_env / job.id).exists()  # worktree cleaned up


async def _die_mid_round(store: MemoryJobStore, repo: Path) -> str:
    """Engine #1 starts a job, Claude Code reports its session, the engine stops."""
    started = asyncio.Event()

    async def hanging_runner(inv, *, env, timeout_s, on_event=None):
        on_event(ProgressEvent(kind="init", summary="session started", session_id=SESSION))
        started.set()
        await asyncio.sleep(3600)
        return _result()

    first = _manager(hanging_runner, store=store)
    job = await _start(first, repo, max_rounds=3)
    await asyncio.wait_for(started.wait(), 5)
    await first.shutdown()
    return job.id


async def test_engine_shutdown_leaves_the_job_running_for_resume(git_repo, coding_env):
    store = MemoryJobStore()
    job_id = await _die_mid_round(store, git_repo)

    persisted = await store.get(job_id, TENANT)
    assert persisted.status == JobStatus.RUNNING
    # The session id is persisted as soon as Claude Code announces it, not at
    # round end -- a crash mid-round must still be resumable.
    assert persisted.session_id == SESSION
    assert persisted.rounds == 1


async def test_resume_on_restart_continues_the_same_session(git_repo, coding_env):
    store = MemoryJobStore()
    job_id = await _die_mid_round(store, git_repo)

    runner = FakeRunner(_commit_ok)
    second = _manager(runner, store=store)
    resumed = await second.resume_interrupted(tenant_id=TENANT)
    job = await second.wait(job_id, TENANT, timeout_s=20)

    assert resumed == 1
    assert job.status == JobStatus.DONE, job.error
    assert runner.calls[0].resume_session_id == SESSION
    assert "restarted" in runner.calls[0].prompt.lower()
    assert job.rounds == 2  # the resume counted as a round


async def test_resume_skips_jobs_that_already_finished(git_repo, coding_env):
    store = MemoryJobStore()
    mgr = _manager(FakeRunner(_commit_ok), store=store)
    await mgr.wait((await _start(mgr, git_repo)).id, TENANT, timeout_s=20)

    again = _manager(FakeRunner(), store=store)
    assert await again.resume_interrupted(tenant_id=TENANT) == 0


async def test_followup_on_a_finished_job_reopens_it(git_repo, coding_env):
    runner = FakeRunner(_commit_ok, _nothing)
    mgr = _manager(runner)
    job = await mgr.wait((await _start(mgr, git_repo)).id, TENANT, timeout_s=20)
    assert job.status == JobStatus.DONE

    await mgr.followup(job.id, TENANT, "also add a docstring")
    job = await mgr.wait(job.id, TENANT, timeout_s=20)

    assert job.status == JobStatus.DONE
    assert runner.calls[1].resume_session_id == SESSION
    assert "also add a docstring" in runner.calls[1].prompt


async def test_concurrency_is_capped_per_tenant(git_repo, coding_env):
    release = asyncio.Event()
    running = 0
    peak = 0

    async def slow_runner(inv, *, env, timeout_s, on_event=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        _commit_ok(Path(inv.cwd))
        running -= 1
        return _result()

    mgr = _manager(slow_runner, max_concurrent=1)
    a = await _start(mgr, git_repo)
    b = await _start(mgr, git_repo)
    await asyncio.sleep(0.3)

    assert (await mgr.get(b.id, TENANT)).status == JobStatus.QUEUED
    release.set()
    a = await mgr.wait(a.id, TENANT, timeout_s=20)
    b = await mgr.wait(b.id, TENANT, timeout_s=20)
    assert a.status == b.status == JobStatus.DONE
    assert peak == 1

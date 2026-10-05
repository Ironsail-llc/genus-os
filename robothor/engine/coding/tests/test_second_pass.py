"""A read-only job's optional second pass: one more round in the same session.

A review that finished well under its turn allowance is asked, once, to
re-check its work and return the full answer again. The final structured
output is the second round's; a second round that fails or returns nothing
leaves the first round's answer in place, and the job is still done.
"""

from __future__ import annotations

from typing import Any

from robothor.engine.coding.jobs import Acceptance, JobStatus
from robothor.engine.coding.tests.test_jobs import (
    SESSION,
    TENANT,
    FakeRunner,
    _manager,
    _nothing,
    _result,
    _start,
)

PASS = "completeness pass: re-check and return the full JSON again"


class Scripted:
    """Results per round, in order."""

    def __init__(self, *results: dict[str, Any]) -> None:
        self.results = list(results)

    def __call__(self):
        return _result(**self.results.pop(0))


async def _review(mgr, repo, acceptance: Acceptance):
    job = await _start(
        mgr,
        repo,
        mode="review",
        acceptance=acceptance,
        json_schema={"type": "object"},
        max_turns=80,
        max_rounds=2,
    )
    return await mgr.wait(job.id, TENANT, timeout_s=20)


def test_acceptance_round_trips_the_second_pass():
    a = Acceptance(
        verify_command=None, require_commit=False, second_pass=PASS, second_pass_below_turns=48
    )
    again = Acceptance.from_dict(a.to_dict())
    assert again.second_pass == PASS and again.second_pass_below_turns == 48
    assert Acceptance.from_dict({}).second_pass is None


async def test_a_quick_first_round_gets_one_second_pass(git_repo, coding_env):
    runner = FakeRunner(
        _nothing,
        _nothing,
        result=Scripted(
            {"num_turns": 20, "structured_output": {"n": 1}},
            {"num_turns": 10, "structured_output": {"n": 2}},
        ),
    )
    mgr = _manager(runner)
    job = await _review(
        mgr,
        git_repo,
        Acceptance(require_commit=False, second_pass=PASS, second_pass_below_turns=48),
    )
    assert job.status == JobStatus.DONE, job.error
    assert job.rounds == 2 and len(runner.calls) == 2
    assert runner.calls[1].resume_session_id == SESSION
    assert PASS in runner.calls[1].prompt
    assert job.result["structured_output"] == {"n": 2}
    assert job.result["second_pass"] == "done"


async def test_a_long_first_round_is_not_asked_again(git_repo, coding_env):
    runner = FakeRunner(_nothing, result=Scripted({"num_turns": 60, "structured_output": {"n": 1}}))
    mgr = _manager(runner)
    job = await _review(
        mgr,
        git_repo,
        Acceptance(require_commit=False, second_pass=PASS, second_pass_below_turns=48),
    )
    assert job.status == JobStatus.DONE and job.rounds == 1
    assert job.result["structured_output"] == {"n": 1}
    assert job.result.get("second_pass") == "skipped"


async def test_a_failed_second_pass_keeps_the_first_answer(git_repo, coding_env):
    runner = FakeRunner(
        _nothing,
        _nothing,
        result=Scripted(
            {"num_turns": 20, "structured_output": {"n": 1}},
            {"num_turns": 80, "is_error": True, "subtype": "error_max_turns"},
        ),
    )
    mgr = _manager(runner)
    job = await _review(
        mgr,
        git_repo,
        Acceptance(require_commit=False, second_pass=PASS, second_pass_below_turns=48),
    )
    assert job.status == JobStatus.DONE, job.error
    assert job.rounds == 2
    assert job.result["structured_output"] == {"n": 1}
    assert job.result["second_pass"] == "incomplete"


async def test_without_a_second_pass_nothing_changes(git_repo, coding_env):
    runner = FakeRunner(_nothing, result=Scripted({"num_turns": 5, "structured_output": {"n": 1}}))
    mgr = _manager(runner)
    job = await _review(mgr, git_repo, Acceptance(require_commit=False))
    assert job.status == JobStatus.DONE and job.rounds == 1
    assert "second_pass" not in job.result


async def test_a_code_job_ignores_the_second_pass(git_repo, coding_env):
    """The second pass is for read-only answers; code jobs are judged by their verify."""
    from robothor.engine.coding.tests.test_jobs import VERIFY, _commit_ok

    runner = FakeRunner(_commit_ok)
    mgr = _manager(runner)
    job = await _start(
        mgr,
        git_repo,
        acceptance=Acceptance(verify_command=VERIFY, second_pass=PASS, second_pass_below_turns=99),
    )
    job = await mgr.wait(job.id, TENANT, timeout_s=20)
    assert job.status == JobStatus.DONE and job.rounds == 1


async def test_the_job_records_the_model_claude_code_used(git_repo, coding_env):
    used = {"num_turns": 5, "structured_output": {"n": 1}, "model": "claude-opus-5-5"}
    runner = FakeRunner(_nothing, result=Scripted(used))
    mgr = _manager(runner)
    job = await _review(mgr, git_repo, Acceptance(require_commit=False))
    assert job.result["model"] == "claude-opus-5-5"

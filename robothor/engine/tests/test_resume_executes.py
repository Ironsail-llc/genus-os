"""Resume must RUN the run, not just charge it for the privilege.

2026-08-27, found by adversarial review after I had already reported this
feature as working. `resume_interrupted_runs` charged `resume_attempts += 1`,
logged "Resuming run X (agent Y, attempt 1/3)", incremented a counter, and
returned it — so the daemon printed "Startup: resumed 3 interrupted agent runs"
having resumed nothing. The runs stayed `cancelled` forever.

I verified the feature from that log line and from resume_attempts moving.
Both are produced BY the hollow loop. The only honest evidence is the run
actually advancing, which is what these tests assert.

The runner was not available at the call site — it is constructed ~35 lines
later, after EngineConfig.from_env() — which is the likely reason the loop was
left as a stub. The ordering constraint that matters is resume-before-reap, not
resume-before-runner, so the block moves down rather than the stub staying.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from robothor.engine import daemon


class TestTheLoopActuallyExecutes:
    @pytest.mark.asyncio
    async def test_a_resumable_run_is_handed_to_the_runner(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RESUME_IN_FLIGHT", "1")

        from robothor.engine.resume import ResumeCandidate

        candidate = ResumeCandidate(
            run_id="11111111-2222-3333-4444-555555555555",
            agent_id="devops-analyst",
            resume_attempts=0,
            has_checkpoint=True,
        )
        # Patch the real seams the function uses, not invented ones: an
        # attribute that does not exist would monkeypatch nothing and let this
        # test quietly exercise the production database.
        import robothor.engine.resume as resume_mod

        monkeypatch.setattr(resume_mod, "resume_batch", lambda c: [candidate])
        monkeypatch.setattr(daemon, "_resume_scan", lambda: [candidate], raising=False)
        monkeypatch.setattr(daemon, "_charge_resume_attempt", lambda rid: True)

        runner = AsyncMock()
        started = await daemon.resume_interrupted_runs(runner)
        # The run is LAUNCHED, not awaited inline — the daemon is still coming
        # up. Yield once so the task actually reaches the runner.
        await asyncio.sleep(0)

        assert started == 1
        assert runner.execute.await_count == 1, "the run was counted but never executed"
        kwargs = runner.execute.await_args.kwargs
        assert kwargs.get("resume_from_run_id") == candidate.run_id
        assert kwargs.get("agent_id") == candidate.agent_id

    @pytest.mark.asyncio
    async def test_nothing_is_counted_when_there_is_no_runner(self, monkeypatch):
        """A missing runner must report 0, not a fictional success."""
        monkeypatch.setenv("ROBOTHOR_RESUME_IN_FLIGHT", "1")
        assert await daemon.resume_interrupted_runs(None) == 0


class TestTheLoopCannotGoHollowAgain:
    """Source-anchored, in the style of test_plugin_groups_are_consumed.

    A behavioural test with a mock runner is necessary but not sufficient: the
    exact failure here was a loop body with no call in it at all, which no
    amount of asserting on a spy that is never reached would have caught.
    """

    def test_the_resume_loop_contains_a_runner_execute_call(self):
        src = (Path(__file__).resolve().parents[1] / "daemon.py").read_text()
        tree = ast.parse(src)
        fns = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and (n.name == "resume_interrupted_runs" or n.name.startswith("_execute_resume"))
        ]
        assert fns, "resume_interrupted_runs is gone"
        # Must be `runner.execute`, AWAITED. The first draft of this guard
        # matched any `.execute(` and passed on the loop's `cur.execute(...)`
        # SQL call — a false pass on the very hollow loop it was written to
        # catch, and the fourth guard in this change to nearly certify the bug.
        awaited_runner_calls = {
            n.value.func.value.id + "." + n.value.func.attr
            for fn in fns
            for n in ast.walk(fn)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and isinstance(n.value.func.value, ast.Name)
        }
        assert "runner.execute" in awaited_runner_calls, (
            "resume_interrupted_runs does not await runner.execute — it is a counter again. "
            f"awaited calls found: {sorted(awaited_runner_calls) or 'none'}"
        )

    def test_the_daemon_passes_a_runner_to_resume(self):
        src = (Path(__file__).resolve().parents[1] / "daemon.py").read_text()
        assert "resume_interrupted_runs(runner)" in src, (
            "resume is called without a runner, so it cannot execute anything"
        )


class TestResumeIsASystemAction:
    """MANUAL is interactive and gets REJECTED without a verified identity.

    This is the second way the same feature failed silently. After the loop was
    made to actually call the runner, every resumed run was still refused at
    runner.py:583 — "Rejected interactive run without verified identity" — which
    returns normally, writes no row, and raises nothing. The daemon logged
    "resumed 3" a second time while resuming nothing, and only a direct probe
    of the execute path surfaced it.
    """

    def test_the_trigger_type_is_a_system_type(self):
        from robothor.engine.models import TriggerType
        from robothor.engine.runner import _SYSTEM_TRIGGER_TYPES

        src = (Path(__file__).resolve().parents[1] / "daemon.py").read_text()
        fn = src.split("async def _execute_resume", 1)[-1].split("\nasync def ", 1)[0]
        used = [t for t in TriggerType if f"TriggerType.{t.name}" in fn]
        assert used, "no trigger type named in _execute_resume"
        for t in used:
            assert t in _SYSTEM_TRIGGER_TYPES, (
                f"resume uses {t.name}, which is interactive — runner.py:583 rejects it "
                f"without a verified identity and the daemon reports success anyway"
            )

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
            tenant_id="tenant-a",
        )
        # Patch the real seams the function uses, not invented ones: an
        # attribute that does not exist would monkeypatch nothing and let this
        # test quietly exercise the production database.
        import robothor.engine.resume as resume_mod

        monkeypatch.setattr(resume_mod, "resume_batch", lambda c: [candidate])
        monkeypatch.setattr(daemon, "_resume_scan", lambda tenant: [candidate])
        monkeypatch.setattr(daemon, "_charge_resume_attempt", lambda rid, tenant: True)

        from unittest.mock import Mock

        monkeypatch.setattr("robothor.engine.resume_claim.acquire", lambda *args: Mock())
        runner = AsyncMock()
        from types import SimpleNamespace

        runner.config = SimpleNamespace(tenant_id="tenant-a")
        started = await daemon.resume_interrupted_runs(runner)
        # The run is LAUNCHED, not awaited inline — the daemon is still coming
        # up. Yield once so the task actually reaches the runner.
        await asyncio.sleep(0)

        assert started == 1
        assert runner.execute.await_count == 1, "the run was counted but never executed"
        kwargs = runner.execute.await_args.kwargs
        assert kwargs.get("resume_from_run_id") == candidate.run_id
        assert kwargs.get("agent_id") == candidate.agent_id
        assert kwargs["tenant_id"] == "tenant-a"

    def test_the_scan_is_a_real_seam_that_returns_a_list(self, monkeypatch):
        """`_resume_scan` must exist and swallow a dead database.

        The test above patched it with `raising=False`, which patches NOTHING
        when the attribute is absent — the scan then ran for real against
        whatever database the test host had. The seam has to be an attribute
        that is actually there, and it has to return [] rather than raise when
        the DB is unreachable, because resume runs at daemon startup.
        """
        import robothor.db.connection as conn_mod

        def _dead(*a, **kw):
            raise OSError("no database")

        monkeypatch.setattr(conn_mod, "get_connection", _dead)
        assert daemon._resume_scan("tenant-a") == []

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


def test_resume_scan_filters_durable_stops_before_checkpoint_access(monkeypatch):
    from unittest.mock import MagicMock, Mock

    from robothor.engine import daemon

    connection = MagicMock()
    cursor = connection.return_value.__enter__.return_value.cursor.return_value
    cursor.fetchall.return_value = [
        ("stopped", "main", 0, "daemon_restart", "tenant-a"),
        ("allowed", "main", 0, "daemon_restart", "tenant-a"),
    ]
    monkeypatch.setattr("robothor.db.connection.get_connection", connection)
    stopped = Mock(side_effect=lambda tenant, run: run == "stopped")
    monkeypatch.setattr("robothor.engine.runtime.controls.stopped", stopped)
    checkpoint = Mock(return_value={"messages": []})
    monkeypatch.setattr("robothor.engine.checkpoint.CheckpointManager.load_latest", checkpoint)
    candidates = daemon._resume_scan("tenant-a")
    assert [candidate.run_id for candidate in candidates] == ["allowed"]
    assert stopped.call_args_list[0].args == ("tenant-a", "stopped")
    checkpoint.assert_called_once_with("allowed", tenant_id="tenant-a")
    assert cursor.execute.call_args.args[1][0] == "tenant-a"
    assert "WHERE tenant_id = %s" in cursor.execute.call_args.args[0]


@pytest.mark.parametrize("matched", [0, 1])
def test_resume_charge_requires_a_row_in_the_same_tenant(monkeypatch, matched):
    from unittest.mock import MagicMock

    from robothor.engine import daemon

    connection = MagicMock()
    cursor = connection.return_value.__enter__.return_value.cursor.return_value
    cursor.rowcount = matched
    monkeypatch.setattr("robothor.db.connection.get_connection", connection)
    assert daemon._charge_resume_attempt("run", "tenant-a") is bool(matched)
    statement, parameters = cursor.execute.call_args.args
    assert "WHERE id = %s AND tenant_id = %s" in statement
    assert parameters == ("run", "tenant-a")


@pytest.mark.asyncio
async def test_cancel_before_resume_worker_starts_releases_claim(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from robothor.engine.resume import ResumeCandidate

    monkeypatch.setenv("ROBOTHOR_RESUME_IN_FLIGHT", "true")
    candidate = ResumeCandidate("run", "main", 0, True, tenant_id="fixture")
    monkeypatch.setattr(daemon, "_resume_scan", lambda tenant: [candidate])
    monkeypatch.setattr(daemon, "_charge_resume_attempt", lambda *args: True)
    claim = Mock()
    monkeypatch.setattr("robothor.engine.resume_claim.acquire", lambda *args: claim)
    runner = SimpleNamespace(config=SimpleNamespace(tenant_id="fixture"), execute=AsyncMock())
    assert await daemon.resume_interrupted_runs(runner) == 1
    tasks = list(daemon._RESUME_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    runner.execute.assert_not_awaited()
    claim.close.assert_called()

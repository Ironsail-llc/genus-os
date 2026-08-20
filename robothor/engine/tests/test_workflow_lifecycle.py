"""Workflow lifecycle tests — no orphans, honest statuses, failures page.

Covers the workflow-health findings (2026-08 diagnosis):

  * Engine shutdown mid-run left workflow_runs rows 'running' forever —
    execute() had no CancelledError finalizer and no reaper covered
    workflow_runs (29 immortal orphans, oldest 171 days).
  * Workflow-deadline exhaustion was misclassified as 'failed' with the
    misleading message 'Run cancelled externally' (113 rows all-time).
  * WorkflowStepDef.retry_count was parsed but never honored.
  * A failed cron workflow produced zero pages and zero notifications —
    its only trace was a line in an LLM-generated briefing.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import workflow as workflow_mod
from robothor.engine.models import (
    RunStatus,
    WorkflowDef,
    WorkflowRun,
    WorkflowStepDef,
    WorkflowStepResult,
    WorkflowStepStatus,
    WorkflowStepType,
)
from robothor.engine.workflow import WorkflowEngine


def _make_engine(engine_config, steps: list[WorkflowStepDef], timeout_seconds: int = 900):
    """WorkflowEngine with one registered workflow and mocked persistence."""
    engine = WorkflowEngine(engine_config, runner=MagicMock())
    wf = WorkflowDef(id="test-wf", name="Test WF", steps=steps, timeout_seconds=timeout_seconds)
    engine._workflows["test-wf"] = wf
    engine._persist_run_start = MagicMock()  # type: ignore[method-assign]
    engine._persist_run_end = MagicMock()  # type: ignore[method-assign]
    engine._persist_step = MagicMock()  # type: ignore[method-assign]
    return engine


def _agent_step(step_id: str = "s1", **kwargs: Any) -> WorkflowStepDef:
    return WorkflowStepDef(
        id=step_id,
        type=WorkflowStepType.AGENT,
        agent_id="test-agent",
        message="hello",
        **kwargs,
    )


def _failed_result(step_id: str = "s1", error: str = "boom") -> WorkflowStepResult:
    return WorkflowStepResult(
        step_id=step_id,
        step_type=WorkflowStepType.AGENT,
        status=WorkflowStepStatus.FAILED,
        error_message=error,
    )


def _completed_result(step_id: str = "s1") -> WorkflowStepResult:
    return WorkflowStepResult(
        step_id=step_id,
        step_type=WorkflowStepType.AGENT,
        status=WorkflowStepStatus.COMPLETED,
        output_text="ok",
    )


class _FakeRegistry:
    """Task-registry stand-in that records spawned coroutines."""

    def __init__(self) -> None:
        self.spawned: list[tuple[Any, str | None]] = []

    def spawn(self, coro: Any, *, name: str | None = None) -> None:
        self.spawned.append((coro, name))


def _dedup_patches():
    return (
        patch("robothor.engine.dedup.try_acquire", new=AsyncMock(return_value=True)),
        patch("robothor.engine.dedup.release", new=AsyncMock()),
    )


# ── 1. Cancellation mid-run must leave a terminal row ───────────────────


class TestCancelledRunFinalized:
    @pytest.mark.asyncio
    async def test_cancel_mid_execute_persists_cancelled_status(self, engine_config) -> None:
        engine = _make_engine(engine_config, [_agent_step()])

        async def _hang(run: WorkflowRun, wf: WorkflowDef) -> None:
            await asyncio.sleep(30)

        engine._execute_steps = _hang  # type: ignore[method-assign]

        acquire, release = _dedup_patches()
        with acquire, release:
            task = asyncio.create_task(engine.execute("test-wf", "cron", "shutdown-test"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        engine._persist_run_end.assert_called_once()
        run = engine._persist_run_end.call_args.args[0]
        assert run.status == RunStatus.CANCELLED
        assert run.error_message == "Cancelled: engine shutdown mid-run"
        assert run.completed_at is not None
        assert run.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_cancelled_run_does_not_page(self, engine_config) -> None:
        """Shutdown cancellation is benign — it must not fire the failure alert."""
        engine = _make_engine(engine_config, [_agent_step()])

        async def _hang(run: WorkflowRun, wf: WorkflowDef) -> None:
            await asyncio.sleep(30)

        engine._execute_steps = _hang  # type: ignore[method-assign]

        registry = _FakeRegistry()
        acquire, release = _dedup_patches()
        with (
            acquire,
            release,
            patch("robothor.engine.task_registry.get_task_registry", return_value=registry),
            patch("robothor.crm.dal.send_notification") as mock_notify,
        ):
            task = asyncio.create_task(engine.execute("test-wf", "cron", "shutdown-test"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert registry.spawned == []
        mock_notify.assert_not_called()


# ── 2. Startup/periodic reaper covers workflow_runs ─────────────────────


class _FakeCursor:
    def __init__(self, rowcount: int = 0) -> None:
        self.executed: list[str] = []
        self.rowcount = rowcount

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)

    def fetchall(self) -> list[Any]:
        return []


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self, **_kwargs: Any) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class TestStaleWorkflowRunReaper:
    def test_cleanup_stale_runs_reaps_workflow_runs(self) -> None:
        from robothor.engine import daemon

        cursor = _FakeCursor(rowcount=3)
        conn = _FakeConn(cursor)
        with patch("robothor.db.connection.get_connection", return_value=conn):
            reaped = daemon._cleanup_stale_runs()

        wf_updates = [s for s in cursor.executed if "UPDATE workflow_runs" in s]
        assert wf_updates, "expected _cleanup_stale_runs to reap workflow_runs too"
        sql = wf_updates[0]
        assert "status='timeout'" in sql
        assert "Reaped: engine restarted mid-run" in sql
        assert "status='running'" in sql
        assert "2 hours" in sql
        assert conn.commits >= 1
        # 3 workflow rows reaped, no stale agent runs
        assert reaped == 3

    def test_workflow_reap_failure_does_not_break_agent_reap(self) -> None:
        from robothor.engine import daemon

        with patch("robothor.db.connection.get_connection", side_effect=Exception("no db")):
            assert daemon._cleanup_stale_runs() == 0


# ── 3. Deadline exhaustion reclassified from FAILED to TIMEOUT ──────────


class TestTimeoutReclassification:
    @pytest.mark.asyncio
    async def test_failed_run_at_deadline_becomes_timeout(self, engine_config) -> None:
        engine = _make_engine(engine_config, [_agent_step()], timeout_seconds=2)

        async def _fail_at_deadline(run: WorkflowRun, wf: WorkflowDef) -> None:
            # Simulate the real misclassification: the workflow deadline
            # cancelled the agent step, the runner swallowed the cancel and
            # reported a step failure, and the whole budget was consumed.
            assert run.started_at is not None
            run.started_at = run.started_at - timedelta(seconds=5)
            result = _failed_result(error="Run cancelled externally; last activity: tool:x")
            run.step_results.append(result)
            run.status = RunStatus.FAILED
            run.error_message = f"Step 's1' failed: {result.error_message}"

        engine._execute_steps = _fail_at_deadline  # type: ignore[method-assign]

        acquire, release = _dedup_patches()
        with acquire, release:
            run = await engine.execute("test-wf", "manual", user_id="u", user_role="owner")

        assert run.status == RunStatus.TIMEOUT
        assert run.error_message is not None
        assert "Timed out after 2s" in run.error_message
        assert "s1" in run.error_message

    @pytest.mark.asyncio
    async def test_fast_failure_stays_failed(self, engine_config) -> None:
        engine = _make_engine(engine_config, [_agent_step()], timeout_seconds=900)

        async def _fail_fast(run: WorkflowRun, wf: WorkflowDef) -> None:
            result = _failed_result(error="boom")
            run.step_results.append(result)
            run.status = RunStatus.FAILED
            run.error_message = "Step 's1' failed: boom"

        engine._execute_steps = _fail_fast  # type: ignore[method-assign]

        acquire, release = _dedup_patches()
        with acquire, release:
            run = await engine.execute("test-wf", "manual", user_id="u", user_role="owner")

        assert run.status == RunStatus.FAILED
        assert "boom" in (run.error_message or "")


# ── 4. retry_count honored with backoff ─────────────────────────────────


class TestStepRetries:
    def test_backoff_pattern_is_60_then_300(self) -> None:
        assert workflow_mod._retry_delay(0) == 60
        assert workflow_mod._retry_delay(1) == 300
        assert workflow_mod._retry_delay(5) == 300

    @pytest.mark.asyncio
    async def test_failed_step_retried_until_success(self, engine_config, monkeypatch) -> None:
        monkeypatch.setattr(workflow_mod, "_RETRY_BACKOFF_SECONDS", (0, 0))
        engine = _make_engine(engine_config, [_agent_step(retry_count=2)])
        engine._execute_step = AsyncMock(  # type: ignore[method-assign]
            side_effect=[_failed_result(), _failed_result(), _completed_result()]
        )

        acquire, release = _dedup_patches()
        with acquire, release:
            run = await engine.execute("test-wf", "manual", user_id="u", user_role="owner")

        assert engine._execute_step.await_count == 3
        assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_retries_exhausted_leaves_failed(self, engine_config, monkeypatch) -> None:
        monkeypatch.setattr(workflow_mod, "_RETRY_BACKOFF_SECONDS", (0, 0))
        engine = _make_engine(engine_config, [_agent_step(retry_count=2)])
        engine._execute_step = AsyncMock(  # type: ignore[method-assign]
            side_effect=[_failed_result(), _failed_result(), _failed_result()]
        )

        acquire, release = _dedup_patches()
        with acquire, release:
            run = await engine.execute("test-wf", "manual", user_id="u", user_role="owner")

        assert engine._execute_step.await_count == 3
        assert run.status == RunStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_retry_by_default(self, engine_config) -> None:
        engine = _make_engine(engine_config, [_agent_step()])
        engine._execute_step = AsyncMock(  # type: ignore[method-assign]
            side_effect=[_failed_result()]
        )

        acquire, release = _dedup_patches()
        with acquire, release:
            run = await engine.execute("test-wf", "manual", user_id="u", user_role="owner")

        assert engine._execute_step.await_count == 1
        assert run.status == RunStatus.FAILED


# ── 5. Failed cron workflow pages the operator + writes a notification ──


class TestFailureAlerting:
    async def _run_failed(
        self, engine_config, trigger: str
    ) -> tuple[WorkflowRun, _FakeRegistry, MagicMock, AsyncMock]:
        engine = _make_engine(engine_config, [_agent_step()])

        async def _fail(run: WorkflowRun, wf: WorkflowDef) -> None:
            result = _failed_result(error="All models failed to respond")
            run.step_results.append(result)
            run.status = RunStatus.FAILED
            run.error_message = f"Step 's1' failed: {result.error_message}"

        engine._execute_steps = _fail  # type: ignore[method-assign]

        registry = _FakeRegistry()
        mock_alert = AsyncMock(return_value=True)
        acquire, release = _dedup_patches()
        with (
            acquire,
            release,
            patch("robothor.engine.task_registry.get_task_registry", return_value=registry),
            patch("robothor.engine.alerts.alert", new=mock_alert),
            patch("robothor.crm.dal.send_notification", return_value="notif-1") as mock_notify,
        ):
            kwargs = {} if trigger in {"cron", "hook"} else {"user_id": "u", "user_role": "owner"}
            run = await engine.execute("test-wf", trigger, **kwargs)
            # Drive any spawned alert coroutine to completion
            for coro, _name in registry.spawned:
                await coro
        return run, registry, mock_notify, mock_alert

    @pytest.mark.asyncio
    async def test_cron_failure_spawns_critical_alert(self, engine_config) -> None:
        run, registry, mock_notify, mock_alert = await self._run_failed(engine_config, "cron")

        assert run.status == RunStatus.FAILED
        assert len(registry.spawned) == 1
        mock_alert.assert_awaited_once()
        args = mock_alert.await_args.args
        assert args[0] == "critical"
        assert "test-wf" in args[1]
        assert "All models failed" in args[2]

    @pytest.mark.asyncio
    async def test_cron_failure_writes_notification_row(self, engine_config) -> None:
        run, _registry, mock_notify, _alert = await self._run_failed(engine_config, "cron")

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["from_agent"] == "engine"
        assert kwargs["to_agent"] == "main"
        # Migration 099 added 'workflow_failure' to the crm_agent_notifications
        # CHECK constraint, so the row uses the precise type directly.
        assert kwargs["notification_type"] == "workflow_failure"
        assert kwargs["metadata"]["kind"] == "workflow_failure"
        assert kwargs["metadata"]["workflow_id"] == "test-wf"
        assert "test-wf" in kwargs["subject"]

    @pytest.mark.asyncio
    async def test_manual_failure_does_not_page(self, engine_config) -> None:
        _run, registry, mock_notify, mock_alert = await self._run_failed(engine_config, "manual")

        assert registry.spawned == []
        mock_notify.assert_not_called()
        mock_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_timeout_pages_too(self, engine_config) -> None:
        engine = _make_engine(engine_config, [_agent_step()], timeout_seconds=1)

        async def _hang(run: WorkflowRun, wf: WorkflowDef) -> None:
            await asyncio.sleep(30)

        engine._execute_steps = _hang  # type: ignore[method-assign]

        registry = _FakeRegistry()
        mock_alert = AsyncMock(return_value=True)
        acquire, release = _dedup_patches()
        with (
            acquire,
            release,
            patch("robothor.engine.task_registry.get_task_registry", return_value=registry),
            patch("robothor.engine.alerts.alert", new=mock_alert),
            patch("robothor.crm.dal.send_notification", return_value="notif-1") as mock_notify,
        ):
            run = await engine.execute("test-wf", "cron")
            for coro, _name in registry.spawned:
                await coro

        assert run.status == RunStatus.TIMEOUT
        assert len(registry.spawned) == 1
        mock_alert.assert_awaited_once()
        mock_notify.assert_called_once()


# ── 7. Dangling agent references warned at load time ────────────────────


class TestAgentReferenceValidation:
    def test_load_workflows_warns_on_unknown_agent(self, engine_config, tmp_path, caplog) -> None:
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "broken.yaml").write_text(
            "id: broken-wf\n"
            "name: Broken\n"
            "steps:\n"
            "  - id: review\n"
            "    type: agent\n"
            "    agent_id: no-such-agent\n"
            "    message: go\n"
        )

        engine = WorkflowEngine(engine_config, runner=MagicMock())
        with caplog.at_level("WARNING"):
            loaded = engine.load_workflows(wf_dir)

        assert loaded == 1  # warn, don't reject — workflow still loads
        warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
        joined = " | ".join(warnings)
        assert "no-such-agent" in joined
        assert "Config validation" in joined

    def test_load_workflows_no_warning_for_known_agent(
        self, engine_config, tmp_path, caplog
    ) -> None:
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "ok.yaml").write_text(
            "id: ok-wf\n"
            "name: OK\n"
            "steps:\n"
            "  - id: review\n"
            "    type: agent\n"
            "    agent_id: known-agent\n"
            "    message: go\n"
        )
        (engine_config.manifest_dir / "known-agent.yaml").write_text(
            "id: known-agent\nname: Known\nmodel:\n  primary: openrouter/test/model\n"
        )

        engine = WorkflowEngine(engine_config, runner=MagicMock())
        with caplog.at_level("WARNING"):
            loaded = engine.load_workflows(wf_dir)

        assert loaded == 1
        assert "no registered agent" not in " | ".join(str(r.message) for r in caplog.records)
